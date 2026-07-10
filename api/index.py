import os
import re
import time
import json
import random
import secrets
import html as html_lib
import unicodedata
from collections import Counter
from datetime import date, datetime, timezone, timedelta
import httpx
from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

app = FastAPI()
security = HTTPBasic()

AC_PHONE_NUMBER_ID = os.environ["AC_PHONE_NUMBER_ID"]
AC_ACCESS_TOKEN = os.environ["AC_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
WEBHOOK_VERIFY_TOKEN = os.environ["WEBHOOK_VERIFY_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

PD_TEMPLATE_NAME = os.environ.get("PD_TEMPLATE_NAME", "pildora_diaria")
PD_TEMPLATE_LANG = os.environ.get("PD_TEMPLATE_LANG", "es_CO")
PD_IMAGEN_URL = os.environ.get("PD_IMAGEN_URL", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
AUDITORIA_SECRET = os.environ.get("AUDITORIA_SECRET", "")
AC_TEMPLATE_NAME = os.environ.get("AC_TEMPLATE_NAME", "primer_contacto")
AC_TEMPLATE_LANG = os.environ.get("AC_TEMPLATE_LANG", "es_CO")
PDF_SERVICE_URL = os.environ.get("PDF_SERVICE_URL", "")
PDF_SERVICE_SECRET = os.environ.get("PDF_SERVICE_SECRET", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_NUMERO = os.environ.get("ADMIN_NUMERO", "")

WA_API_VERSION = "v20.0"
GEMINI_MODELS_FALLBACK = [
    m.strip() for m in os.environ.get(
        "GEMINI_MODELS_FALLBACK", "gemini-flash-latest,gemini-pro-latest"
    ).split(",") if m.strip()
]


async def _llamar_gemini(contents: list, system_instruction: str, tools: list, temperatura: float, max_tokens: int):
    """Prueba cada modelo de GEMINI_MODELS_FALLBACK en orden hasta que uno responda.
    Devuelve (texto, modelo_usado) o (None, None) si todos fallan."""
    payload_base = {"contents": contents, "systemInstruction": {"parts": [{"text": system_instruction}]}}
    if tools:
        payload_base["tools"] = tools

    async with httpx.AsyncClient() as client:
        for modelo in GEMINI_MODELS_FALLBACK:
            config = {"temperature": temperatura, "maxOutputTokens": max_tokens}
            # Los modelos "pro" no permiten desactivar el modo pensamiento (thinkingBudget 0),
            # solo los "flash" lo soportan.
            if "flash" in modelo.lower():
                config["thinkingConfig"] = {"thinkingBudget": 0}
            payload = {**payload_base, "generationConfig": config}
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={GEMINI_API_KEY}"
            try:
                res = await client.post(url, json=payload, timeout=45.0)
                if res.status_code == 200:
                    data = res.json()
                    candidates = data.get("candidates")
                    if candidates and candidates[0].get("content", {}).get("parts"):
                        texto = candidates[0]["content"]["parts"][0].get("text", "")
                        if texto:
                            return texto, modelo
                    print(f"⚠️ {modelo} respondió 200 sin texto útil, probando siguiente modelo")
                    continue
                print(f"⚠️ {modelo} falló [{res.status_code}], probando siguiente modelo: {res.text[:200]}")
            except Exception as e:
                print(f"⚠️ {modelo} lanzó excepción, probando siguiente modelo: {e}")
    return None, None


SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


@app.get("/api")
@app.get("/webhook")
def verificar_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == WEBHOOK_VERIFY_TOKEN:
        return Response(content=params.get("hub.challenge"), media_type="text/plain")
    return Response(content="Forbidden", status_code=403)


@app.post("/api")
@app.post("/webhook")
async def recibir_mensaje(request: Request):
    try:
        body = await request.json()
        entrada = _extraer_valor(body)
        if not entrada:
            return _ok()
        if "statuses" in entrada and "messages" not in entrada:
            return _ok()

        messages = entrada.get("messages", [])
        if not messages:
            return _ok()

        msg = messages[0]
        msg_id = msg.get("id")

        if msg_id:
            if await ya_procesado(msg_id):
                return _ok()
            await marcar_procesado(msg_id)

        if msg.get("type") == "text":
            numero = str(msg.get("from")).strip()
            texto_usuario = (msg.get("text", {}).get("body") or "").strip()
            if texto_usuario:
                await procesar_flujo_bot(numero, texto_usuario)

        elif msg.get("type") == "button":
            numero = str(msg.get("from")).strip()
            boton = msg.get("button", {})
            payload_boton = boton.get("payload") or ""
            if payload_boton.startswith("PILDORA_VER_"):
                await enviar_pildora_al_asesor(numero)
            elif (boton.get("text") or "") == "Continuar":
                await procesar_aceptacion_por_numero(numero)

        elif msg.get("type") == "interactive":
            numero = str(msg.get("from")).strip()
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                button_id = interactive.get("button_reply", {}).get("id", "")
                if button_id == "pildora_aplicare":
                    await registrar_aplicare(numero)
                elif button_id == "btn_recibida":
                    await marcar_recibida_auditoria(numero)
                elif button_id.startswith("ACEPTAR_"):
                    await procesar_aceptacion_consolidada(numero, button_id.replace("ACEPTAR_", "", 1))
                else:
                    await procesar_aceptacion_por_numero(numero)

    except Exception as e:
        print(f"❌ Error controlado en recibir_mensaje: {e}")

    return _ok()


def _extraer_valor(body):
    try:
        return body["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return None


def _ok():
    return Response(content='{"status":"success"}', media_type="application/json")


async def procesar_flujo_bot(numero: str, texto: str):
    if ADMIN_NUMERO and numero == ADMIN_NUMERO and texto.strip().lower().startswith("#nueva"):
        contenido = texto.strip()[len("#nueva"):].strip()
        if contenido:
            await guardar_conocimiento_extra(contenido)
            await despachar_mensaje_whatsapp(numero, f"✅ Guardado en la base de conocimiento:\n\n_{contenido}_")
        else:
            await despachar_mensaje_whatsapp(numero, "Escribí *#nueva* seguido del contenido a guardar. Ej: `#nueva el horario de atención cambió a partir de agosto`")
        return

    if await procesar_compromiso_consolidado(numero, texto):
        return
    if await guardar_compromiso_auditoria(numero, texto):
        return
    if await procesar_respuesta_metricas(numero, texto):
        return

    nombre_asesora = await buscar_asesora(numero)
    if not nombre_asesora:
        respuesta = await gestionar_nuevo_usuario(numero, texto)
    elif _es_pregunta_metricas(texto):
        respuesta = await iniciar_solicitud_metricas(numero)
    else:
        await marcar_escribiendo_whatsapp(numero)
        respuesta = await consultar_du_bot(texto, nombre_asesora, numero)
    await despachar_mensaje_whatsapp(numero, respuesta)


# ============================================================
# SUPABASE: asesoras / registro pendiente / historial / dedup / manuales
# ============================================================

async def _sb_get(tabla: str, params: dict):
    async with httpx.AsyncClient() as client:
        res = await client.get(f"{SUPABASE_URL}/rest/v1/{tabla}", headers=SUPABASE_HEADERS, params=params, timeout=10.0)
        res.raise_for_status()
        return res.json()


async def _sb_post(tabla: str, payload):
    headers = {**SUPABASE_HEADERS, "Prefer": "return=minimal"}
    async with httpx.AsyncClient() as client:
        await client.post(f"{SUPABASE_URL}/rest/v1/{tabla}", headers=headers, json=payload, timeout=10.0)


async def _sb_delete(tabla: str, params: dict):
    async with httpx.AsyncClient() as client:
        await client.delete(f"{SUPABASE_URL}/rest/v1/{tabla}", headers=SUPABASE_HEADERS, params=params, timeout=10.0)


async def _sb_patch(tabla: str, params: dict, payload: dict):
    headers = {**SUPABASE_HEADERS, "Prefer": "return=minimal"}
    async with httpx.AsyncClient() as client:
        await client.patch(f"{SUPABASE_URL}/rest/v1/{tabla}", headers=headers, params=params, json=payload, timeout=10.0)


async def _sb_upsert(tabla: str, payload):
    headers = {**SUPABASE_HEADERS, "Prefer": "return=minimal,resolution=merge-duplicates"}
    async with httpx.AsyncClient() as client:
        await client.post(f"{SUPABASE_URL}/rest/v1/{tabla}", headers=headers, json=payload, timeout=10.0)


async def buscar_asesora(numero: str):
    filas = await _sb_get("asesoras", {
        "or": f"(numero.eq.{numero},numero_alt.eq.{numero})",
        "select": "nombre",
        "limit": "1",
    })
    return filas[0]["nombre"] if filas else None


async def _extraer_nombre(texto: str):
    """Extrae el nombre real de la respuesta del usuario, aunque venga como frase
    completa ('me llamo Ana', 'soy Carlos Perez') y no solo el nombre suelto."""
    extraido, _ = await _llamar_gemini(
        contents=[{"role": "user", "parts": [{"text": texto}]}],
        system_instruction=(
            "El usuario te va a responder con su nombre, a veces como frase completa "
            "(ej. 'me llamo Ana', 'soy Carlos Perez', 'mi nombre es X') y a veces solo el nombre suelto. "
            "Respondé ÚNICAMENTE con el nombre completo de la persona, sin nada más, sin puntuación "
            "extra, respetando mayúsculas. Si no podés identificar un nombre real en el texto, "
            "respondé exactamente: SIN_NOMBRE"
        ),
        tools=None,
        temperatura=0.0,
        max_tokens=30,
    )
    if not extraido or extraido.strip() == "SIN_NOMBRE":
        return None
    return extraido.strip()


async def gestionar_nuevo_usuario(numero: str, texto: str) -> str:
    pendientes = await _sb_get("registro_pendiente", {"numero": f"eq.{numero}", "select": "numero"})
    if pendientes:
        nombre = await _extraer_nombre(texto)
        if not nombre:
            return ("No logré identificar tu nombre en ese mensaje 🤔 ¿Me lo podés escribir de nuevo, "
                    "solo tu nombre completo?")
        await _sb_post("asesoras", {"nombre": nombre, "numero": numero, "cargo": "Asesora Nueva"})
        await _sb_delete("registro_pendiente", {"numero": f"eq.{numero}"})
        return (f"¡Listo! Ya te registré en mis contactos como *{nombre}* ¡Qué alegría tenerte en el equipo! 🙌✨ "
                f"Ahora sí, dime con total confianza, ¿en qué te puedo ayudar hoy con los procesos de Cofrem? 🧠🚀")
    else:
        await _sb_post("registro_pendiente", {"numero": numero})
        return ("¡Hola! ¡Qué gusto saludarte! 😊👋 Veo que me estás escribiendo desde un número nuevo y aún no te "
                "tengo en mi lista de contactos. ¿Me podrías decir tu nombre completo para registrarte por aquí? ✨")


async def obtener_historial(numero: str):
    filas = await _sb_get("historial_conversaciones", {
        "numero": f"eq.{numero}",
        "select": "rol,texto",
        "order": "created_at.desc",
        "limit": "12",
    })
    return list(reversed(filas))


async def guardar_historial(numero: str, pregunta: str, respuesta: str):
    await _sb_post("historial_conversaciones", [
        {"numero": numero, "rol": "user", "texto": pregunta},
        {"numero": numero, "rol": "model", "texto": respuesta},
    ])


async def ya_procesado(msg_id: str) -> bool:
    filas = await _sb_get("mensajes_procesados", {"msg_id": f"eq.{msg_id}", "select": "msg_id"})
    return len(filas) > 0


async def marcar_procesado(msg_id: str):
    await _sb_post("mensajes_procesados", {"msg_id": msg_id})


STOPWORDS = {
    "el", "la", "los", "las", "de", "del", "un", "una", "unos", "unas", "en", "y", "o", "que",
    "es", "son", "para", "por", "con", "se", "su", "sus", "al", "a", "como", "cual", "cuales",
    "me", "mi", "tu", "le", "les", "lo", "esta", "este", "esto", "esos", "esas", "hay", "ya",
}


def _normalizar(texto: str) -> set:
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    palabras = re.findall(r"[a-z0-9]+", texto)
    return {p for p in palabras if len(p) > 2 and p not in STOPWORDS}


async def obtener_manuales(pregunta: str):
    filas = await _sb_get("manuales_gemini", {"select": "id_gemini,nombre_archivo"})
    palabras_pregunta = _normalizar(pregunta)

    relevantes = []
    for f in filas:
        palabras_archivo = _normalizar(f["nombre_archivo"])
        coincidencias = len(palabras_pregunta & palabras_archivo)
        if coincidencias > 0:
            relevantes.append((coincidencias, f))

    relevantes.sort(key=lambda x: x[0], reverse=True)
    seleccionados = [f for _, f in relevantes[:4]]

    return [
        {"file_data": {
            "mime_type": "application/pdf",
            "file_uri": f"https://generativelanguage.googleapis.com/v1beta/{f['id_gemini']}",
        }}
        for f in seleccionados
    ]


PALABRAS_TARIFA = {
    "precio", "precios", "tarifa", "tarifas", "cuesta", "cuestan", "vale", "valen",
    "cuanto", "valor", "cobra", "cobran", "costo", "costos",
}


async def buscar_tarifas(pregunta: str):
    palabras_pregunta = _normalizar(pregunta)
    if not palabras_pregunta & PALABRAS_TARIFA:
        return []

    filas = await _sb_get("tarifas", {"select": "producto,precio"})

    relevantes = []
    for f in filas:
        palabras_producto = _normalizar(f["producto"])
        coincidencias = len(palabras_pregunta & palabras_producto)
        if coincidencias > 0:
            relevantes.append((coincidencias, f))

    relevantes.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in relevantes[:8]]


async def guardar_conocimiento_extra(contenido: str):
    await _sb_post("conocimiento_extra", {"contenido": contenido})


async def buscar_conocimiento_extra(pregunta: str):
    palabras_pregunta = _normalizar(pregunta)
    if not palabras_pregunta:
        return []

    filas = await _sb_get("conocimiento_extra", {"select": "contenido"})

    relevantes = []
    for f in filas:
        palabras_contenido = _normalizar(f["contenido"])
        coincidencias = len(palabras_pregunta & palabras_contenido)
        if coincidencias > 0:
            relevantes.append((coincidencias, f["contenido"]))

    relevantes.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in relevantes[:5]]


# ============================================================
# CEREBRO DE DU — 1 sola llamada a Gemini con prioridad de fuentes + memoria
# ============================================================

async def consultar_du_bot(mensaje_usuario: str, nombre_asesora: str, numero: str) -> str:
    historial = await obtener_historial(numero)
    archivos_parts = await obtener_manuales(mensaje_usuario)
    conocimiento_extra = await buscar_conocimiento_extra(mensaje_usuario)
    tarifas_encontradas = await buscar_tarifas(mensaje_usuario)

    system_instruction = (
        "Tu nombre es Du. Eres el compañero de trabajo virtual de las asesoras de COFREM en People BPO, "
        "ayudándolas en tiempo real mientras atienden llamadas o chats.\n\n"
        f"Te habla: *{nombre_asesora}*, tu compañera de equipo. Trátala con calidez y compañerismo, sin formalidad excesiva.\n\n"
        "REGLA #1 - LÍMITE DURO DE ALCANCE (la más importante, nunca la rompas):\n"
        "SOLO existís para ayudar con temas de COFREM: afiliaciones, subsidios, créditos, trámites, normativa laboral "
        "relacionada, portafolio de servicios, etc. Punto.\n"
        "Si la pregunta es sobre CUALQUIER otro tema (geografía, cultura general, entretenimiento, otras empresas, clima, "
        "noticias, matemáticas, programación, o cualquier cosa que no sea Cofrem) — NO la respondas, sin importar que sepas "
        "la respuesta o que la búsqueda web te la traiga. En vez de responderla, contestá algo como: \"Uy, eso se sale de "
        "mi área 😅 Yo solo manejo temas de Cofrem. ¿Te ayudo con algo de un trámite o proceso?\" (podés variar el tono "
        "pero NUNCA contestar el dato en sí).\n\n"
        "ORDEN DE PRIORIDAD DE FUENTES para preguntas que SÍ son de Cofrem (decide tú misma cuál usar, sin avisar el proceso):\n"
        "1. Si la pregunta es sobre tarifas o precios de un servicio, y te paso un bloque \"TARIFAS ENCONTRADAS\" junto con "
        "el mensaje, esa es la fuente autoritativa y exacta — respondé con eso, no busques en otro lado. Si el bloque viene "
        "vacío o no trae el servicio/categoría exacta que preguntan, decilo honestamente en vez de inventar un precio.\n"
        "2. Si te paso un bloque \"AVISOS INTERNOS\" junto con el mensaje, y tiene relación con la pregunta, esa es "
        "información reciente cargada directamente por el coordinador — priorizala sobre los PDFs y la web si aplica.\n"
        "3. Para todo lo demás, revisa primero los documentos PDF adjuntos (manuales internos oficiales). Si el dato está "
        "ahí, respóndelo basado en eso.\n"
        "4. Si no está en los documentos, usa la búsqueda web pero confía SOLO en resultados de cofrem.com.co.\n"
        "5. Si tampoco encuentras nada ahí, podés buscar en la web general, pero SIEMPRE específicamente sobre COFREM "
        "(nunca sobre otras cajas de compensación como Comfama, Compensar, Cafam, Colsubsidio, etc. — aunque el trámite o "
        "tema exista en cualquier caja, la respuesta tiene que ser la versión y las reglas propias de Cofrem, nunca la de otra "
        "caja aunque parezca aplicar igual). Aclará en tu respuesta que es información general no oficial y que debe validarse.\n"
        "6. Si de verdad no encontrás nada sobre Cofrem en ninguna fuente, decilo honestamente: no inventes, no asumas, y "
        "nunca completes con información de otra caja de compensación aunque sea parecida.\n\n"
        "RECENCIA DE FUENTES WEB (aplica a los pasos 3 y 4, cuando buscás fuera de los PDF adjuntos):\n"
        "Preferí siempre resultados de 2025 o 2026. Si la fuente más confiable que encontrás es más vieja (2024, 2023 o "
        "anterior), igual podés usarla, pero tenés que aclararlo en la respuesta, algo como \"(fuente año 2023, puede haber "
        "cambiado)\" — nunca presentes un dato viejo como si fuera vigente sin avisar.\n\n"
        f"REGLA DE ORO - BREVEDAD: {nombre_asesora} probablemente está EN VIVO con un usuario esperando. "
        "Responde en máximo 3-4 líneas o 3 puntos clave.\n\n"
        "FORMATO WHATSAPP:\n"
        "- Negrita con UN SOLO asterisco: *así* (nunca doble asterisco).\n"
        "- Listas cortas con guiones (-), máximo 3 puntos.\n"
        "- Sin encabezados tipo Markdown (#, ##).\n\n"
        "COMPORTAMIENTO:\n"
        "- Si la pregunta es ambigua, pide amablemente más detalle antes de responder.\n"
        "- Al final de cada respuesta con información real, pregunta breve: \"¿Te quedó claro o profundizamos en algo? 💬\"\n"
        "- Si te saluda o agradece brevemente, responde corto y animado sin repetir esa pregunta.\n"
        "- Usa el historial de la conversación para entender el contexto de preguntas de seguimiento.\n"
        "- Si hay historial de conversación reciente (ya veías mensajes previos con esta persona), NO vuelvas a saludar con "
        "\"¡Hola, [nombre]!\" en cada respuesta — eso es solo para el primer mensaje de una conversación nueva. En una "
        "pregunta de seguimiento andá directo al grano, como seguiría hablando alguien que ya está en la conversación.\n"
        "- NUNCA anuncies \"voy a buscar en tal parte\" ni menciones tu proceso interno de búsqueda: responde directo con la información."
    )

    if tarifas_encontradas:
        lineas_tarifas = "\n".join(f"- {t['producto']}: {t['precio']}" for t in tarifas_encontradas)
        texto_tarifas = f"\n\nTARIFAS ENCONTRADAS:\n{lineas_tarifas}"
    else:
        texto_tarifas = ""

    if conocimiento_extra:
        lineas_avisos = "\n".join(f"- {c}" for c in conocimiento_extra)
        texto_avisos = f"\n\nAVISOS INTERNOS:\n{lineas_avisos}"
    else:
        texto_avisos = ""

    contenido_historial = [{"role": h["rol"], "parts": [{"text": h["texto"]}]} for h in historial]
    contents = contenido_historial + [
        {"role": "user", "parts": archivos_parts + [{"text": mensaje_usuario + texto_tarifas + texto_avisos}]}
    ]

    texto_respuesta, modelo_usado = await _llamar_gemini(
        contents=contents,
        system_instruction=system_instruction,
        tools=[{"google_search": {}}],
        temperatura=0.4,
        max_tokens=1024,
    )

    if not texto_respuesta:
        return f"Hola {nombre_asesora}. Tuve un contratiempo de conexión. Dame un minutito e intenta de nuevo. 🛠️✨"

    await guardar_historial(numero, mensaje_usuario, texto_respuesta)
    return texto_respuesta


# ============================================================
# WHATSAPP CLOUD API
# ============================================================

async def despachar_mensaje_whatsapp(numero: str, texto: str):
    url = f"https://graph.facebook.com/{WA_API_VERSION}/{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {"messaging_product": "whatsapp", "to": numero, "type": "text", "text": {"body": texto}}
    async with httpx.AsyncClient() as client:
        res = await client.post(url, json=payload, headers=headers, timeout=15.0)
        if res.status_code != 200:
            print(f"❌ Error enviando mensaje: [{res.status_code}] {res.text}")


async def marcar_escribiendo_whatsapp(numero: str):
    url = f"https://graph.facebook.com/{WA_API_VERSION}/{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "sender_action": "typing_on",
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload, headers=headers, timeout=10.0)


# ============================================================
# PÍLDORAS DIARIAS
# ============================================================

CATEGORIAS_OPERATIVAS = [
    "Empatía", "Comunicación efectiva", "Saludo y cierre", "Manejo de objeciones",
    "Mindset positivo", "Procedimientos COFREM", "Motivación", "Buenas prácticas",
]
CATEGORIAS_MOTIVADORAS = [
    "Motivación", "Inspiración", "Buen día", "Productividad", "Bienestar", "Crecimiento",
]
CATEGORIAS_LIDERAZGO = [
    "Motivación personal", "Bienestar", "Mindset", "Crecimiento personal",
    "Inspiración", "Reflexión", "Paz mental", "Perspectiva",
]
AREAS_SIN_ATENCION = {"radicacion", "encuestas", "administrativo", "administrativa", "administrativos"}
PALABRAS_LIDERAZGO = {
    "coordinador", "coordinadora", "lider", "jefe", "jefa",
    "supervisor", "supervisora", "gerente", "auditor", "auditora",
}
FESTIVOS_2026 = {
    "2026-06-29", "2026-07-20", "2026-08-07", "2026-08-17", "2026-10-12",
    "2026-11-02", "2026-11-16", "2026-12-08", "2026-12-25",
}
DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def identificar_tipo_area(area: str) -> str:
    area_norm = "".join(c for c in unicodedata.normalize("NFKD", area.lower()) if not unicodedata.combining(c))
    if any(p in area_norm for p in PALABRAS_LIDERAZGO):
        return "liderazgo"
    if any(a in area_norm for a in AREAS_SIN_ATENCION):
        return "motivadora"
    return "operativa"


def seleccionar_categoria(tipo_area: str) -> str:
    categorias = {
        "liderazgo": CATEGORIAS_LIDERAZGO,
        "motivadora": CATEGORIAS_MOTIVADORAS,
    }.get(tipo_area, CATEGORIAS_OPERATIVAS)
    return random.choice(categorias)


def es_festivo(fecha: date) -> bool:
    return fecha.isoformat() in FESTIVOS_2026


async def generar_pildora_gemini(area: str, categoria: str, tipo_area: str):
    if tipo_area == "liderazgo":
        system_prompt = (
            "Eres un coach de vida y motivación personal.\n\n"
            "Tu tarea: generar UNA frase motivadora BREVE para una persona adulta profesional.\n\n"
            "REGLAS:\n"
            "1. Máximo 4 líneas (50-60 palabras)\n"
            "2. Tono cercano, humano, NO corporativo\n"
            "3. NO menciones trabajo, liderazgo, equipo, oficina, productividad\n"
            "4. Enfoque en: motivación personal, bienestar, mindset, paz mental, crecimiento, perspectiva\n"
            "5. Que la persona se sienta mejor consigo misma al leerla\n"
            "6. NO uses markdown\n"
            f"7. Categoría: {categoria}\n"
            "8. Texto plano, conversacional, como si fuera un mensaje de un amigo sabio\n"
            "9. NO empieces con \"Querido líder\" ni nada formal\n\n"
            "Responde SOLO con la frase. NO incluyas títulos, saludos, ni firma."
        )
        user_prompt = f"Genera una píldora de \"{categoria}\" de motivación personal y bienestar."
    elif tipo_area == "operativa":
        canal = "chat" if "chat" in area.lower() else "llamadas"
        system_prompt = (
            "Eres un experto en formación y calidad para agentes de servicio al cliente de COFREM "
            "(Caja de Compensación Familiar del Meta, Colombia).\n\n"
            f"Tu tarea: generar UNA píldora educativa BREVE y PRÁCTICA para un agente de \"{area}\" que atiende "
            f"usuarios por {canal}.\n\n"
            "REGLAS:\n"
            "1. Máximo 4 líneas (60 palabras)\n"
            "2. Tono cercano, no formal en exceso\n"
            "3. Da un tip ACCIONABLE (algo que pueda aplicar HOY)\n"
            "4. NO uses markdown\n"
            "5. NO menciones COFREM en cada píldora\n"
            f"6. Categoría: {categoria}\n"
            f"7. Adaptada al canal: {canal}\n"
            "8. Texto plano\n\n"
            "Responde SOLO con la píldora. NO incluyas títulos, saludos, ni firma."
        )
        user_prompt = f"Genera una píldora de \"{categoria}\" para agentes de {canal}."
    else:
        dia_actual = DIAS_SEMANA[date.today().weekday()]
        system_prompt = (
            "Eres un amigo cercano y con humor que manda un mensaje motivador por WhatsApp.\n\n"
            "Tu tarea: generar UNA frase motivadora BREVE sobre la VIDA en general — nunca sobre trabajo, oficina, "
            "tareas o productividad.\n\n"
            "REGLAS:\n"
            "1. Máximo 3 líneas (40-50 palabras)\n"
            "2. Tono relajado, con humor y picardía, como le hablarías a un amigo — nada de lenguaje corporativo\n"
            f"3. Categoría: {categoria}\n"
            f"4. Hoy es {dia_actual}: si encaja con la categoría, aprovechá el día con humor (ej. un viernes puede "
            "ser algo como \"Por fin viernes y el cuerpo lo sabe jaja\"; un lunes algo sobre lo que cuesta arrancar "
            "la semana), pero no lo fuerces si no pega\n"
            "5. NO uses markdown\n"
            "6. PROHIBIDO mencionar trabajo, oficina, equipo, jefe, productividad, metas o procesos — es un mensaje "
            "de vida, no de trabajo\n"
            "7. Texto plano\n\n"
            "Responde SOLO con la frase motivadora. NO incluyas títulos ni firma."
        )
        user_prompt = f"Genera una píldora de \"{categoria}\" de motivación de vida, teniendo en cuenta que hoy es {dia_actual}."

    texto, modelo_usado = await _llamar_gemini(
        contents=[{"role": "user", "parts": [{"text": user_prompt}]}],
        system_instruction=system_prompt,
        tools=None,
        temperatura=0.8,
        max_tokens=300,
    )
    return texto.strip() if texto else None


async def enviar_plantilla_pildora(numero: str, pildora: str, categoria: str, area: str) -> bool:
    url = f"https://graph.facebook.com/{WA_API_VERSION}/{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "template",
        "template": {
            "name": PD_TEMPLATE_NAME,
            "language": {"code": PD_TEMPLATE_LANG},
            "components": [
                {"type": "header", "parameters": [{"type": "image", "image": {"link": PD_IMAGEN_URL}}]},
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "0",
                    "parameters": [{"type": "payload", "payload": f"PILDORA_VER_{int(time.time())}"}],
                },
            ],
        },
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(url, json=payload, headers=headers, timeout=15.0)
        if res.status_code != 200:
            print(f"❌ Error enviando plantilla píldora a {numero}: [{res.status_code}] {res.text}")
            return False

    await _sb_upsert("pildoras_pendientes", {
        "numero": numero, "pildora": pildora, "categoria": categoria, "area": area,
    })
    return True


async def enviar_pildora_al_asesor(numero: str):
    filas = await _sb_get("pildoras_pendientes", {"numero": f"eq.{numero}", "select": "*"})
    if not filas:
        return

    data = filas[0]
    mensaje = (
        "🎓 *Píldora del día*\n\n"
        f"📚 {data['categoria']}\n\n"
        f"{data['pildora']}\n\n"
        "_— Du Academy_"
    )
    await enviar_mensaje_interactivo(numero, mensaje)


async def enviar_mensaje_interactivo(numero: str, texto: str):
    url = f"https://graph.facebook.com/{WA_API_VERSION}/{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": [{"type": "reply", "reply": {"id": "pildora_aplicare", "title": "👍 Lo aplicaré"}}]},
        },
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(url, json=payload, headers=headers, timeout=15.0)
        if res.status_code != 200:
            print(f"❌ Error enviando interactivo: [{res.status_code}] {res.text}")


async def registrar_aplicare(numero: str):
    filas = await _sb_get("pildoras_pendientes", {"numero": f"eq.{numero}", "select": "area"})
    if filas:
        area = filas[0]["area"]
        hoy = date.today().isoformat()
        filas_hoy = await _sb_get("pildoras_enviadas", {
            "fecha": f"eq.{hoy}", "area": f"eq.{area}", "select": "id,aplicaran",
        })
        if filas_hoy:
            fila = filas_hoy[0]
            nuevo_valor = (fila.get("aplicaran") or 0) + 1
            await _sb_patch("pildoras_enviadas", {"id": f"eq.{fila['id']}"}, {"aplicaran": nuevo_valor})

    nombre = await buscar_asesora(numero) or ""
    saludo = f"¡Genial {nombre}! 🙌" if nombre else "¡Genial! 🙌"
    mensaje = f"{saludo}\n\nTu compromiso de aplicarla quedó registrado.\n\nQue tengas un excelente día. ☀️"
    await despachar_mensaje_whatsapp(numero, mensaje)


async def enviar_pildora_del_dia():
    hoy = date.today()
    if hoy.weekday() == 6:
        print("⏭️ Hoy es domingo, no se envían píldoras")
        return
    if es_festivo(hoy):
        print("⏭️ Hoy es festivo, no se envían píldoras")
        return

    asesoras = await _sb_get("asesoras", {"select": "nombre,numero,area"})
    asesoras = [a for a in asesoras if a.get("area")]
    if not asesoras:
        print("⚠️ No hay asesoras con área asignada")
        return

    grupos = {}
    for a in asesoras:
        grupos.setdefault(a["area"], []).append(a)

    for area, lista in grupos.items():
        tipo_area = identificar_tipo_area(area)
        categoria = seleccionar_categoria(tipo_area)
        pildora = await generar_pildora_gemini(area, categoria, tipo_area)
        if not pildora:
            print(f"❌ No se pudo generar píldora para {area}")
            continue

        enviados = 0
        for asesora in lista:
            ok = await enviar_plantilla_pildora(asesora["numero"], pildora, categoria, area)
            if ok:
                enviados += 1

        await _sb_post("pildoras_enviadas", {
            "fecha": hoy.isoformat(), "area": area, "categoria": categoria,
            "pildora": pildora, "total_enviadas": enviados, "aplicaran": 0,
        })
        print(f"✅ {enviados} píldoras enviadas para {area}")


@app.get("/api/cron-pildoras")
async def cron_pildoras(request: Request):
    if CRON_SECRET and request.headers.get("authorization") != f"Bearer {CRON_SECRET}":
        return Response(content="Forbidden", status_code=403)
    await enviar_pildora_del_dia()
    return _ok()


# ============================================================
# CHEQUEO DE SALUD DIARIO — avisa por WhatsApp si algo se rompió
# ============================================================

@app.get("/api/cron-healthcheck")
async def cron_healthcheck(request: Request):
    if CRON_SECRET and request.headers.get("authorization") != f"Bearer {CRON_SECRET}":
        return Response(content="Forbidden", status_code=403)

    problemas = []

    # 1. WhatsApp: el token y el numero siguen respondiendo
    try:
        url = f"https://graph.facebook.com/{WA_API_VERSION}/{AC_PHONE_NUMBER_ID}?fields=verified_name"
        headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=headers, timeout=15.0)
        if res.status_code != 200:
            problemas.append(f"WhatsApp: token o número con error [{res.status_code}] {res.text[:150]}")
    except Exception as e:
        problemas.append(f"WhatsApp: excepción al verificar ({e})")

    # 2. Gemini: al menos un modelo de la cadena de respaldo debe responder
    texto, modelo_usado = await _llamar_gemini(
        contents=[{"role": "user", "parts": [{"text": "responde solo con OK"}]}],
        system_instruction="Responde solo con la palabra OK, nada más.",
        tools=None,
        temperatura=0.0,
        max_tokens=100,
    )
    if not texto:
        problemas.append("Gemini: ningún modelo de la cadena de respaldo respondió (todos caídos)")
    elif modelo_usado != GEMINI_MODELS_FALLBACK[0]:
        problemas.append(
            f"Gemini: el modelo principal ({GEMINI_MODELS_FALLBACK[0]}) sigue sin responder, "
            f"usando respaldo ({modelo_usado}) — el bot funciona pero más lento"
        )

    if problemas and ADMIN_NUMERO:
        mensaje = "🚨 *Chequeo diario del bot* — se encontraron problemas:\n\n" + "\n".join(f"- {p}" for p in problemas)
        await despachar_mensaje_whatsapp(ADMIN_NUMERO, mensaje)

    if problemas:
        print(f"⚠️ Healthcheck con problemas: {problemas}")
    else:
        print("✅ Healthcheck OK")

    return Response(content=json.dumps({"ok": not problemas, "problemas": problemas}), media_type="application/json")


# ============================================================
# AUDITORÍAS — envío por WhatsApp, botón "Recibida" y compromiso
# ============================================================

async def enviar_auditoria(numero: str, nombre_asesora: str, nota: int, hallazgos: list, puntos_mejora: list) -> bool:
    cuerpo = f"*🎯 AUDITORIA - {nombre_asesora}*\n\n"
    cuerpo += f"*📊 Nota: {nota}/100*\n\n"

    cuerpo += "*📌 Hallazgos:*\n"
    if hallazgos:
        for h in hallazgos:
            cuerpo += f"• {h}\n"
    else:
        cuerpo += "• Ninguno - ¡Excelente desempeño!\n"

    cuerpo += "\n*⚡ Puntos de Mejora:*\n"
    if puntos_mejora:
        for p in puntos_mejora:
            cuerpo += f"• {p}\n"
    else:
        cuerpo += "• Mantén el estándar actual\n"

    cuerpo += "\n_¿Recibiste la auditoría?_"

    url = f"https://graph.facebook.com/{WA_API_VERSION}/{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": cuerpo},
            "action": {"buttons": [{"type": "reply", "reply": {"id": "btn_recibida", "title": "✅ Recibida"}}]},
        },
    }

    async with httpx.AsyncClient() as client:
        res = await client.post(url, json=payload, headers=headers, timeout=15.0)
        if res.status_code != 200:
            print(f"❌ Error enviando auditoría a {numero}: [{res.status_code}] {res.text}")
            return False
        message_id = res.json().get("messages", [{}])[0].get("id")

    await _sb_post("auditorias", {
        "numero": numero,
        "nombre_asesora": nombre_asesora,
        "hallazgos": " | ".join(hallazgos),
        "puntos_mejora": " | ".join(puntos_mejora),
        "nota": nota,
        "estado": "Enviada",
        "message_id": message_id,
    })
    return True


async def marcar_recibida_auditoria(numero: str):
    filas = await _sb_get("auditorias", {
        "numero": f"eq.{numero}", "estado": "eq.Enviada",
        "select": "id", "order": "created_at.desc", "limit": "1",
    })
    if not filas:
        return

    await _sb_patch("auditorias", {"id": f"eq.{filas[0]['id']}"}, {"estado": "Recibida"})

    mensaje = (
        "Gracias por confirmar 👋\n\n"
        "Ahora, por favor escribe el *COMPROMISO DE MEJORA* que te comprometes a cumplir basado en esta auditoría.\n\n"
        "_(Ejemplo: Mejorar el tiempo de respuesta en los primeros 30 segundos de cada llamada)_"
    )
    await despachar_mensaje_whatsapp(numero, mensaje)


async def guardar_compromiso_auditoria(numero: str, texto: str) -> bool:
    filas = await _sb_get("auditorias", {
        "numero": f"eq.{numero}", "estado": "eq.Recibida",
        "select": "id", "order": "created_at.desc", "limit": "1",
    })
    if not filas:
        return False

    await _sb_patch("auditorias", {"id": f"eq.{filas[0]['id']}"}, {
        "estado": "Comprometida",
        "compromiso": texto,
        "fecha_compromiso": _ahora_iso(),
    })

    mensaje = (
        f"✅ *Compromiso registrado:*\n\n\"_{texto}_\"\n\n"
        "Quedará pendiente de cumplimiento en los próximos 30 días.\n\n"
        "¡Éxito en tu mejora continua! 💪"
    )
    await despachar_mensaje_whatsapp(numero, mensaje)
    return True


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.post("/api/enviar-auditoria")
async def api_enviar_auditoria(request: Request):
    if not AUDITORIA_SECRET or request.headers.get("authorization") != f"Bearer {AUDITORIA_SECRET}":
        return Response(content="Forbidden", status_code=403)

    body = await request.json()
    numero = str(body.get("numero", "")).strip()
    nombre = str(body.get("nombre", "")).strip()
    nota = int(body.get("nota", 0))
    hallazgos = body.get("hallazgos", [])
    puntos_mejora = body.get("puntosMejora", [])

    if not numero or not nombre:
        return Response(content=json.dumps({"error": "numero y nombre requeridos"}), status_code=400, media_type="application/json")

    ok = await enviar_auditoria(numero, nombre, nota, hallazgos, puntos_mejora)
    return Response(content=json.dumps({"ok": ok}), media_type="application/json")


# ============================================================
# AUDITORÍAS CONSOLIDADAS — flujo automático desde Cortes_Envio
# (plantilla "primer_contacto", 2 PDFs generados vía Apps Script)
# ============================================================

async def enviar_mensaje_boton_url(numero: str, texto_cuerpo: str, texto_boton: str, url: str):
    endpoint = f"https://graph.facebook.com/{WA_API_VERSION}/{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": texto_cuerpo},
            "action": {"name": "cta_url", "parameters": {"display_text": texto_boton, "url": url}},
        },
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(endpoint, json=payload, headers=headers, timeout=15.0)
        if res.status_code != 200:
            print(f"❌ Error enviando botón URL: [{res.status_code}] {res.text}")


async def solicitar_pdf(datos: dict, tipo: str):
    if not PDF_SERVICE_URL:
        print("❌ PDF_SERVICE_URL no configurado")
        return None
    payload = {"secret": PDF_SERVICE_SECRET, "datos": datos, "tipo": tipo}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            res = await client.post(PDF_SERVICE_URL, json=payload, timeout=45.0)
            if res.status_code != 200:
                print(f"❌ Error generando PDF ({tipo}): [{res.status_code}] {res.text}")
                return None
            data = res.json()
            return data.get("url")
        except Exception as e:
            print(f"❌ Error llamando al servicio de PDF: {e}")
            return None


@app.post("/api/auditoria-consolidada/enviar")
async def api_enviar_auditoria_consolidada(request: Request):
    if not AUDITORIA_SECRET or request.headers.get("authorization") != f"Bearer {AUDITORIA_SECRET}":
        return Response(content="Forbidden", status_code=403)

    body = await request.json()
    id_corte = str(body.get("idCorte", "")).strip()
    numero = str(body.get("numero", "")).strip()
    nombre = str(body.get("nombre", "")).strip()

    if not id_corte or not numero or not nombre:
        return Response(content=json.dumps({"error": "idCorte, numero y nombre requeridos"}), status_code=400, media_type="application/json")

    endpoint = f"https://graph.facebook.com/{WA_API_VERSION}/{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "template",
        "template": {
            "name": AC_TEMPLATE_NAME,
            "language": {"code": AC_TEMPLATE_LANG},
            "components": [
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "0",
                    "parameters": [{"type": "payload", "payload": f"ACEPTAR_{id_corte}"}],
                }
            ],
        },
    }

    async with httpx.AsyncClient() as client:
        res = await client.post(endpoint, json=payload, headers=headers, timeout=15.0)
        if res.status_code != 200:
            print(f"❌ Error enviando plantilla {AC_TEMPLATE_NAME} a {numero}: [{res.status_code}] {res.text}")
            return Response(content=json.dumps({"ok": False, "error": res.text}), status_code=502, media_type="application/json")

    await _sb_post("auditorias_consolidadas", {
        "id_corte": id_corte,
        "numero": numero,
        "nombre_asesora": nombre,
        "usuario": body.get("usuario"),
        "fecha_auditoria": body.get("fecha"),
        "cantidad_auditorias": body.get("cantidadAuditorias"),
        "nota": body.get("nota"),
        "hallazgos": body.get("hallazgos"),
        "puntos_mejora": body.get("puntosMejora"),
        "link_pdf_inicial": body.get("pdfInicialUrl"),
        "estado": "PENDIENTE_ACEPTACION",
    })

    return Response(content=json.dumps({"ok": True}), media_type="application/json")


async def procesar_aceptacion_por_numero(numero: str):
    filas = await _sb_get("auditorias_consolidadas", {
        "numero": f"eq.{numero}", "estado": "eq.PENDIENTE_ACEPTACION",
        "select": "id_corte", "order": "fecha_envio.desc", "limit": "1",
    })
    if filas:
        await procesar_aceptacion_consolidada(numero, filas[0]["id_corte"])


async def procesar_aceptacion_consolidada(numero: str, id_corte: str):
    filas = await _sb_get("auditorias_consolidadas", {
        "id_corte": f"eq.{id_corte}", "select": "nombre_asesora,link_pdf_inicial",
    })
    if not filas:
        return
    datos = filas[0]
    asesor = datos["nombre_asesora"]

    await _sb_patch("auditorias_consolidadas", {"id_corte": f"eq.{id_corte}"}, {
        "estado": "AUDITORIA_LEIDA",
        "fecha_lectura": _ahora_iso(),
    })

    mensaje_auditoria = (
        f"¡Hola {asesor}! 👋\n\n"
        "📋 Tu *informe consolidado de auditoría* ya está listo.\n\n"
        "Presiona el botón para revisarlo. 👇"
    )
    await enviar_mensaje_boton_url(numero, mensaje_auditoria, "Ver Auditoría", datos["link_pdf_inicial"])

    mensaje_compromiso = (
        "📝 *Compromiso de mejora*\n\n"
        "Por favor, responde este mensaje indicando el compromiso que asumirás para aplicar los puntos de mejora identificados.\n\n"
        "_Tu respuesta quedará registrada como evidencia._ ✍️"
    )
    await despachar_mensaje_whatsapp(numero, mensaje_compromiso)


async def procesar_compromiso_consolidado(numero: str, texto: str) -> bool:
    filas = await _sb_get("auditorias_consolidadas", {
        "numero": f"eq.{numero}", "estado": "eq.AUDITORIA_LEIDA",
        "select": "*", "order": "fecha_envio.desc", "limit": "1",
    })
    if not filas:
        return False

    datos = filas[0]
    id_corte = datos["id_corte"]
    asesor = datos["nombre_asesora"]
    fecha_compromiso_iso = _ahora_iso()

    await _sb_patch("auditorias_consolidadas", {"id_corte": f"eq.{id_corte}"}, {
        "compromiso": texto,
        "fecha_compromiso": fecha_compromiso_iso,
        "estado": "COMPROMISO_RECIBIDO",
    })

    await despachar_mensaje_whatsapp(numero, (
        f"¡Listo {asesor}! ✅\n\n"
        "Tu *compromiso de mejora* quedó registrado.\n\n"
        "Confío en que aplicarás los puntos de mejora. 💪"
    ))
    await despachar_mensaje_whatsapp(numero, "📄 En unos segundos recibirás tu *compromiso firmado*...")

    pdf_final_url = await solicitar_pdf({
        "ASESOR": asesor,
        "FECHA_AUDITORIA": datos.get("fecha_auditoria"),
        "ID_CORTE": id_corte,
        "CANTIDAD_AUDITORIAS": datos.get("cantidad_auditorias"),
        "PROMEDIO_NOTA": datos.get("nota"),
        "HALLAZGOS": datos.get("hallazgos"),
        "PUNTOS_MEJORA": datos.get("puntos_mejora"),
        "COMPROMISO": texto,
        "FECHA_COMPROMISO": _formatear_fecha_co(fecha_compromiso_iso),
    }, "FINAL")

    if not pdf_final_url:
        print(f"❌ No se pudo generar el PDF final para {id_corte}")
        return True

    await _sb_patch("auditorias_consolidadas", {"id_corte": f"eq.{id_corte}"}, {
        "link_pdf_final": pdf_final_url,
        "estado": "CERRADA",
        "fecha_cierre": _ahora_iso(),
    })

    await enviar_mensaje_boton_url(
        numero,
        "📑 ¡Aquí está tu *compromiso firmado*!\n\nQue tengas un excelente turno. 🚀✨",
        "Ver Compromiso",
        pdf_final_url,
    )
    return True


def _formatear_fecha_co(iso_str: str) -> str:
    dt = datetime.fromisoformat(iso_str)
    return dt.strftime("%d/%m/%Y")


# ============================================================
# MÉTRICAS EN TIEMPO REAL — consulta con verificación de contraseña
# ============================================================

PATRONES_METRICAS = [
    re.compile(r"\bmis?\s+metricas?\b"),
    re.compile(r"\bcomo\s+voy\b"),
    re.compile(r"\bmi\s+bono\b"),
    re.compile(r"\bmi\s+desempen\w*\b"),
    re.compile(r"\bmi\s+calificacion\b"),
    re.compile(r"\bmis\s+resultados\b"),
    re.compile(r"\bmi\s+productividad\b"),
    re.compile(r"\bmi\s+adherencia\b"),
    re.compile(r"\bmi\s+pec\b"),
    re.compile(r"\bmi\s+penc\b"),
]


def _texto_normalizado_plano(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


def _es_pregunta_metricas(texto: str) -> bool:
    plano = _texto_normalizado_plano(texto)
    return any(p.search(plano) for p in PATRONES_METRICAS)


async def iniciar_solicitud_metricas(numero: str) -> str:
    await _sb_upsert("metricas_pendientes", {"numero": numero})
    return (
        "Claro, para continuar por favor envíame la *contraseña* que te asignaron. 🔐"
    )


async def procesar_respuesta_metricas(numero: str, texto: str) -> bool:
    pendientes = await _sb_get("metricas_pendientes", {"numero": f"eq.{numero}", "select": "numero"})
    if not pendientes:
        return False

    texto_limpio = texto.strip()
    parece_password = (
        0 < len(texto_limpio) <= 20 and " " not in texto_limpio and "?" not in texto_limpio
    )
    if not parece_password:
        # No parece un intento de contraseña (es una pregunta normal, con espacios/signos) —
        # cancelamos el pedido pendiente en vez de bloquear la conversación para siempre.
        await _sb_delete("metricas_pendientes", {"numero": f"eq.{numero}"})
        return False

    filas = await _sb_get("asesoras", {"numero": f"eq.{numero}", "select": "nombre,usuario,contrasena"})
    if not filas:
        return False
    asesora = filas[0]

    if not asesora.get("contrasena") or texto_limpio != asesora["contrasena"]:
        await despachar_mensaje_whatsapp(numero, "Esa contraseña no es correcta 🙈 Intenta de nuevo, o seguí preguntando normal si te equivocaste de comando.")
        return True

    await _sb_delete("metricas_pendientes", {"numero": f"eq.{numero}"})
    await _sb_post("metricas_solicitudes", {"numero": numero, "usuario": asesora.get("usuario")})

    metricas = await _sb_get("metricas_asesoras", {
        "usuario": f"eq.{asesora['usuario']}", "select": "metrica,valor,fecha",
    })

    if not metricas:
        await despachar_mensaje_whatsapp(
            numero,
            f"Hola {asesora['nombre']}, no encontré métricas registradas todavía a tu nombre. Consulta con tu supervisor. 📋",
        )
        return True

    fecha = metricas[0].get("fecha") or ""
    lineas = "\n".join(f"{m['metrica']}: {m['valor']}" for m in metricas)

    imagen_url = await solicitar_imagen_metricas(asesora["nombre"], lineas, fecha)
    if imagen_url:
        await enviar_imagen_whatsapp(numero, imagen_url, "¡Sigue así! 💪")
    else:
        mensaje = (
            f"📊 *Tus métricas, {asesora['nombre']}*"
            + (f" ({fecha})" if fecha else "")
            + f"\n\n{lineas}\n\n¡Sigue así! 💪"
        )
        await despachar_mensaje_whatsapp(numero, mensaje)
    return True


async def solicitar_imagen_metricas(nombre: str, metricas_texto: str, fecha: str):
    if not PDF_SERVICE_URL:
        return None
    payload = {
        "secret": PDF_SERVICE_SECRET,
        "accion": "imagen_metricas",
        "nombre": nombre,
        "metricas": metricas_texto,
        "fecha": fecha,
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            res = await client.post(PDF_SERVICE_URL, json=payload, timeout=45.0)
            if res.status_code != 200:
                print(f"❌ Error generando imagen de métricas: [{res.status_code}] {res.text}")
                return None
            return res.json().get("url")
        except Exception as e:
            print(f"❌ Error llamando al servicio de imagen: {e}")
            return None


async def enviar_imagen_whatsapp(numero: str, image_url: str, caption: str = ""):
    url = f"https://graph.facebook.com/{WA_API_VERSION}/{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "image",
        "image": {"link": image_url, "caption": caption},
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(url, json=payload, headers=headers, timeout=15.0)
        if res.status_code != 200:
            print(f"❌ Error enviando imagen: [{res.status_code}] {res.text}")


# ============================================================
# DASHBOARD ADMIN
# ============================================================

def verificar_admin(credentials: HTTPBasicCredentials = Depends(security)):
    clave_ok = bool(ADMIN_PASSWORD) and secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not clave_ok:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return True


def _barra_html(etiqueta: str, valor: int, maximo: int) -> str:
    pct = round((valor / maximo) * 100) if maximo else 0
    return f"""
    <div class="barra-fila">
      <div class="barra-etiqueta">{html_lib.escape(str(etiqueta))}</div>
      <div class="barra-fondo"><div class="barra-relleno" style="width:{pct}%"></div></div>
      <div class="barra-valor">{valor}</div>
    </div>"""


def _badge_delta(hoy: int, ayer: int) -> str:
    if ayer == 0:
        if hoy == 0:
            return ""
        return '<span class="badge badge-up">▲ nuevo</span>'
    delta = round(((hoy - ayer) / ayer) * 100)
    if delta > 0:
        return f'<span class="badge badge-up">▲ {delta}%</span>'
    if delta < 0:
        return f'<span class="badge badge-down">▼ {abs(delta)}%</span>'
    return '<span class="badge badge-flat">— 0%</span>'


def _dias_desde(iso_str: str) -> int:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 0


_PILL_ESTADO = {
    "PENDIENTE_ACEPTACION": ("Sin abrir", "pill-red"),
    "AUDITORIA_LEIDA": ("Sin compromiso", "pill-amber"),
    "COMPROMISO_RECIBIDO": ("Generando PDF", "pill-blue"),
    "CERRADA": ("Cerrada", "pill-green"),
}


@app.get("/admin")
async def dashboard_admin(ok: bool = Depends(verificar_admin)):
    asesoras = await _sb_get("asesoras", {"select": "numero,nombre,usuario,area"})
    mapa_nombres = {a["numero"]: a["nombre"] for a in asesoras}

    historial = await _sb_get("historial_conversaciones", {
        "rol": "eq.user", "select": "numero,texto,created_at",
        "order": "created_at.desc", "limit": "500",
    })

    auditorias = await _sb_get("auditorias_consolidadas", {
        "select": "nombre_asesora,estado,fecha_envio",
    })
    hoy_str = date.today().isoformat()
    ayer_str = (date.today() - timedelta(days=1)).isoformat()

    pildoras_hoy = await _sb_get("pildoras_enviadas", {
        "fecha": f"eq.{hoy_str}", "select": "area,categoria,total_enviadas,aplicaran",
    })
    pildoras_ayer = await _sb_get("pildoras_enviadas", {
        "fecha": f"eq.{ayer_str}", "select": "total_enviadas",
    })
    solicitudes_metricas = await _sb_get("metricas_solicitudes", {"select": "numero,usuario"})

    # ── Deltas reales hoy vs ayer ──
    mensajes_hoy = sum(1 for h in historial if str(h["created_at"])[:10] == hoy_str)
    mensajes_ayer = sum(1 for h in historial if str(h["created_at"])[:10] == ayer_str)
    badge_mensajes = _badge_delta(mensajes_hoy, mensajes_ayer)

    total_enviadas_hoy = sum(p["total_enviadas"] or 0 for p in pildoras_hoy)
    total_enviadas_ayer = sum(p["total_enviadas"] or 0 for p in pildoras_ayer)
    badge_pildoras = _badge_delta(total_enviadas_hoy, total_enviadas_ayer)

    total_aplicaron_hoy = sum(p["aplicaran"] or 0 for p in pildoras_hoy)
    tasa_aplicacion = round((total_aplicaron_hoy / total_enviadas_hoy) * 100) if total_enviadas_hoy else 0

    # ── Actividad por asesora (para el grafico de barras) ──
    conteo_mensajes = Counter(h["numero"] for h in historial)
    top_actividad = conteo_mensajes.most_common(10)
    labels_actividad = json.dumps([mapa_nombres.get(n, n) for n, _ in top_actividad])
    valores_actividad = json.dumps([c for _, c in top_actividad])

    # ── Preguntas recientes ──
    recientes = historial[:20]
    filas_recientes = "".join(
        f"<tr><td>{html_lib.escape(mapa_nombres.get(h['numero'], h['numero']))}</td>"
        f"<td>{html_lib.escape(h['texto'][:120])}</td>"
        f"<td class='fecha-chica'>{html_lib.escape(str(h['created_at'])[:16].replace('T', ' '))}</td></tr>"
        for h in recientes
    ) or "<tr><td colspan='3' class='vacio'>Sin preguntas todavía.</td></tr>"

    # ── Auditorías por estado ──
    conteo_estados = Counter(a["estado"] for a in auditorias)
    orden_estados = ["PENDIENTE_ACEPTACION", "AUDITORIA_LEIDA", "COMPROMISO_RECIBIDO", "CERRADA"]
    etiquetas_estado = {
        "PENDIENTE_ACEPTACION": "Enviada, sin abrir",
        "AUDITORIA_LEIDA": "Leída, sin compromiso",
        "COMPROMISO_RECIBIDO": "Compromiso recibido",
        "CERRADA": "Cerrada (con PDF final)",
    }
    max_estado = max(conteo_estados.values(), default=1)
    filas_estados = "".join(
        _barra_html(etiquetas_estado.get(e, e), conteo_estados.get(e, 0), max_estado) for e in orden_estados
    )

    # ── Auditorías pendientes — quién falta, ordenado por más vieja primero ──
    pendientes_auditoria = [a for a in auditorias if a["estado"] != "CERRADA"]
    pendientes_auditoria.sort(key=lambda a: a.get("fecha_envio") or "")
    filas_pendientes = "".join(
        (lambda etq, cls: (
            f"<tr><td>{html_lib.escape(a['nombre_asesora'])}</td>"
            f"<td><span class='pill {cls}'>{etq}</span></td>"
            f"<td class='fecha-chica'>{_dias_desde(a['fecha_envio'])} día(s)</td></tr>"
        ))(*_PILL_ESTADO.get(a["estado"], (a["estado"], "pill-blue")))
        for a in pendientes_auditoria
    ) or "<tr><td colspan='3' class='vacio'>Nadie pendiente — todas cerradas ✅</td></tr>"

    # ── Píldoras de hoy — detalle ──
    filas_pildoras = "".join(
        f"<tr><td>{html_lib.escape(p['area'])}</td><td>{html_lib.escape(p['categoria'])}</td>"
        f"<td>{p['total_enviadas']}</td><td>{p['aplicaran']}</td></tr>"
        for p in pildoras_hoy
    ) or "<tr><td colspan='4' class='vacio'>Sin píldoras enviadas hoy.</td></tr>"

    # ── Métricas solicitadas por asesora ──
    conteo_metricas = Counter(s["numero"] for s in solicitudes_metricas)
    top_metricas = conteo_metricas.most_common(15)
    max_metricas = max((c for _, c in top_metricas), default=1)
    filas_metricas = "".join(
        _barra_html(mapa_nombres.get(num, num), cant, max_metricas) for num, cant in top_metricas
    ) or "<p class='vacio'>Nadie consultó sus métricas todavía.</p>"

    hoy_legible = date.today().strftime("%d %b %Y")

    ICON_ASESORAS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
    ICON_CHAT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>'
    ICON_PILDORA = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m10.5 20.5 10-10a4.95 4.95 0 1 0-7-7l-10 10a4.95 4.95 0 1 0 7 7Z"/><path d="m8.5 8.5 7 7"/></svg>'
    ICON_AUDIT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 15l2 2 4-4"/></svg>'

    html_final = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Du Academy — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {{
    --bg:#0a0a0b; --panel:#141416; --panel-2:#1c1c1f; --border:#26262a;
    --lime:#a4d65e; --lime-dim:#7fae3e; --lime-soft:#c8e895; --text:#f5f5f6; --muted:#8f8f96;
    --red:#e2665a; --amber:#e0a83a; --blue:#5b9bd6;
  }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif; background:var(--bg); color:var(--text); margin:0; -webkit-font-smoothing:antialiased; }}

  .topbar {{ display:flex; align-items:center; justify-content:space-between; padding:16px 32px; border-bottom:1px solid var(--border); background:rgba(20,20,22,0.85); backdrop-filter:blur(10px); position:sticky; top:0; z-index:10; }}
  .brand {{ display:flex; align-items:center; gap:10px; font-weight:700; font-size:17px; }}
  .brand .dot {{ width:9px; height:9px; border-radius:50%; background:var(--lime); box-shadow:0 0 10px var(--lime); }}
  .navpills {{ display:flex; gap:6px; background:var(--panel-2); padding:4px; border-radius:12px; }}
  .navpills span {{ padding:8px 16px; border-radius:9px; font-size:13px; color:var(--muted); cursor:default; }}
  .navpills span.active {{ background:var(--panel); color:var(--text); font-weight:600; box-shadow:0 1px 2px rgba(0,0,0,0.3); }}
  .profile {{ display:flex; align-items:center; gap:10px; color:var(--muted); font-size:13px; }}
  .profile .avatar {{ width:32px; height:32px; border-radius:50%; background:var(--lime); color:#0b0b0c; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:13px; }}

  main {{ padding:32px 40px 60px; max-width:1720px; margin:0 auto; width:100%; }}
  .header-row {{ display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:24px; flex-wrap:wrap; gap:10px; }}
  .header-row h1 {{ margin:0; font-size:26px; letter-spacing:-0.4px; }}
  .header-row .fecha {{ color:var(--muted); font-size:13px; }}

  .kpi-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:18px; margin-bottom:22px; }}
  @media (max-width:1100px) {{ .kpi-grid {{ grid-template-columns:repeat(2,1fr); }} }}
  .kpi {{ background:var(--panel); border:1px solid var(--border); border-radius:18px; padding:22px; position:relative; overflow:hidden; transition:transform .15s ease, border-color .15s ease; }}
  .kpi:hover {{ transform:translateY(-2px); border-color:#3a3a40; }}
  .kpi.hero {{ background:linear-gradient(140deg, var(--lime-soft) 0%, var(--lime) 55%, var(--lime-dim) 100%); color:#0b0b0c; border:none; }}
  .kpi-top {{ display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:18px; }}
  .kpi-icon {{ width:38px; height:38px; border-radius:11px; background:rgba(164,214,94,0.12); display:flex; align-items:center; justify-content:center; color:var(--lime); }}
  .kpi-icon svg {{ width:19px; height:19px; }}
  .kpi.hero .kpi-icon {{ background:rgba(0,0,0,0.14); color:#0b0b0c; }}
  .kpi-label {{ font-size:13px; color:var(--muted); margin-bottom:6px; font-weight:500; }}
  .kpi.hero .kpi-label {{ color:rgba(0,0,0,0.62); }}
  .kpi-value {{ font-size:34px; font-weight:800; letter-spacing:-0.8px; }}
  .badge {{ font-size:11px; font-weight:700; padding:4px 9px; border-radius:20px; white-space:nowrap; }}
  .badge-up {{ background:rgba(164,214,94,0.15); color:var(--lime); }}
  .kpi.hero .badge-up {{ background:rgba(0,0,0,0.14); color:#0b0b0c; }}
  .badge-down {{ background:rgba(226,102,90,0.15); color:var(--red); }}
  .badge-flat {{ background:rgba(143,143,150,0.15); color:var(--muted); }}

  .panel-grid {{ display:grid; grid-template-columns:1.4fr 1fr; gap:18px; margin-bottom:18px; }}
  .panel-grid.equal {{ grid-template-columns:1fr 1fr; }}
  @media (max-width:1000px) {{ .panel-grid, .panel-grid.equal {{ grid-template-columns:1fr; }} }}
  .panel {{ background:var(--panel); border:1px solid var(--border); border-radius:18px; padding:24px; }}
  .panel h2 {{ margin:0 0 4px 0; font-size:15px; color:var(--text); font-weight:700; }}
  .panel .subtitulo {{ color:var(--muted); font-size:12px; margin-bottom:18px; }}

  .gauge-wrap {{ display:flex; flex-direction:column; align-items:center; }}
  .gauge-num {{ font-size:36px; font-weight:800; color:var(--lime); margin-top:-72px; letter-spacing:-1px; }}
  .gauge-lbl {{ color:var(--muted); font-size:12px; }}
  .gauge-sub {{ display:flex; justify-content:space-around; width:100%; margin-top:20px; padding-top:18px; border-top:1px solid var(--border); }}
  .gauge-sub div {{ text-align:center; }}
  .gauge-sub .n {{ font-size:19px; font-weight:700; }}
  .gauge-sub .l {{ font-size:11px; color:var(--muted); }}

  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ text-align:left; padding:11px 10px; border-bottom:1px solid var(--border); font-size:13px; }}
  th {{ color:var(--muted); font-weight:600; font-size:10.5px; text-transform:uppercase; letter-spacing:0.06em; }}
  tbody tr:hover {{ background:var(--panel-2); }}
  .fecha-chica {{ color:var(--muted); font-size:12px; white-space:nowrap; }}
  .vacio {{ color:var(--muted); font-style:italic; }}

  .pill {{ display:inline-block; font-size:11px; font-weight:700; padding:4px 10px; border-radius:20px; }}
  .pill-red {{ background:rgba(226,102,90,0.15); color:var(--red); }}
  .pill-amber {{ background:rgba(224,168,58,0.15); color:var(--amber); }}
  .pill-blue {{ background:rgba(91,155,214,0.15); color:var(--blue); }}
  .pill-green {{ background:rgba(164,214,94,0.15); color:var(--lime); }}

  .barra-fila {{ display:flex; align-items:center; gap:10px; margin:9px 0; }}
  .barra-etiqueta {{ width:170px; font-size:13px; flex-shrink:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted); }}
  .barra-fondo {{ flex:1; background:var(--panel-2); border-radius:6px; height:14px; overflow:hidden; }}
  .barra-relleno {{ background:var(--lime); height:100%; border-radius:6px; }}
  .barra-valor {{ width:28px; text-align:right; font-size:13px; color:var(--lime); font-weight:600; }}

  section {{ margin-top:18px; }}
</style>
</head>
<body>
  <div class="topbar">
    <div class="brand"><span class="dot"></span> Du Academy</div>
    <div class="navpills">
      <span class="active">Resumen</span>
      <span>Asesoras</span>
      <span>Auditorías</span>
      <span>Píldoras</span>
      <span>Métricas</span>
    </div>
    <div class="profile"><div class="avatar">D</div> Duvan Ramos</div>
  </div>

  <main>
    <div class="header-row">
      <h1>Resumen general</h1>
      <div class="fecha">{hoy_legible}</div>
    </div>

    <div class="kpi-grid">
      <div class="kpi hero">
        <div class="kpi-top"><div class="kpi-icon">{ICON_ASESORAS}</div></div>
        <div class="kpi-label">Asesoras registradas</div>
        <div class="kpi-value">{len(asesoras)}</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><div class="kpi-icon">{ICON_CHAT}</div>{badge_mensajes}</div>
        <div class="kpi-label">Preguntas al bot hoy</div>
        <div class="kpi-value">{mensajes_hoy}</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><div class="kpi-icon">{ICON_PILDORA}</div>{badge_pildoras}</div>
        <div class="kpi-label">Píldoras enviadas hoy</div>
        <div class="kpi-value">{total_enviadas_hoy}</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><div class="kpi-icon">{ICON_AUDIT}</div></div>
        <div class="kpi-label">Auditorías pendientes</div>
        <div class="kpi-value">{len(pendientes_auditoria)}</div>
      </div>
    </div>

    <div class="panel-grid">
      <div class="panel">
        <h2>Actividad por asesora</h2>
        <div class="subtitulo">Preguntas hechas al bot (top 10)</div>
        <canvas id="graficoActividad" height="220"></canvas>
      </div>
      <div class="panel">
        <h2>Tasa "lo aplicaré"</h2>
        <div class="subtitulo">Píldoras de hoy</div>
        <div class="gauge-wrap">
          <canvas id="graficoGauge" width="220" height="140"></canvas>
          <div class="gauge-num">{tasa_aplicacion}%</div>
          <div class="gauge-lbl">de compromiso</div>
          <div class="gauge-sub">
            <div><div class="n">{total_enviadas_hoy}</div><div class="l">Enviadas</div></div>
            <div><div class="n">{total_aplicaron_hoy}</div><div class="l">Aplicarán</div></div>
          </div>
        </div>
      </div>
    </div>

    <div class="panel-grid equal">
      <section class="panel">
        <h2>Auditorías pendientes — quién falta</h2>
        <div class="subtitulo">Ordenadas por más antigua primero</div>
        <table>
          <tr><th>Asesora</th><th>Estado</th><th>Tiempo esperando</th></tr>
          {filas_pendientes}
        </table>
      </section>
      <section class="panel">
        <h2>Auditorías consolidadas</h2>
        <div class="subtitulo">Distribución por estado (total)</div>
        {filas_estados}
      </section>
    </div>

    <section class="panel">
      <h2>Preguntas recientes</h2>
      <div class="subtitulo">Últimas 20 preguntas al bot conversacional</div>
      <table>
        <tr><th>Asesora</th><th>Pregunta</th><th>Fecha</th></tr>
        {filas_recientes}
      </table>
    </section>

    <div class="panel-grid equal">
      <section class="panel">
        <h2>Consultas de métricas</h2>
        <div class="subtitulo">Por asesora</div>
        {filas_metricas}
      </section>
      <section class="panel">
        <h2>Píldoras enviadas hoy</h2>
        <div class="subtitulo">Detalle por área</div>
        <table>
          <tr><th>Área</th><th>Categoría</th><th>Enviadas</th><th>Aplicarán</th></tr>
          {filas_pildoras}
        </table>
      </section>
    </div>
  </main>

  <script>
    Chart.defaults.color = '#8f8f96';
    Chart.defaults.font.family = "-apple-system, Arial, sans-serif";

    new Chart(document.getElementById('graficoActividad'), {{
      type: 'bar',
      data: {{
        labels: {labels_actividad},
        datasets: [{{ data: {valores_actividad}, backgroundColor: '#a4d65e', borderRadius: 6, maxBarThickness: 34 }}]
      }},
      options: {{
        plugins: {{ legend: {{ display:false }} }},
        scales: {{
          x: {{ grid: {{ display:false }} }},
          y: {{ grid: {{ color:'#26262a' }}, beginAtZero:true, ticks: {{ precision:0 }} }}
        }}
      }}
    }});

    new Chart(document.getElementById('graficoGauge'), {{
      type: 'doughnut',
      data: {{
        datasets: [{{
          data: [{tasa_aplicacion}, {100 - tasa_aplicacion}],
          backgroundColor: ['#a4d65e', '#26262a'],
          borderWidth: 0,
        }}]
      }},
      options: {{
        rotation: -90, circumference: 180, cutout: '75%',
        plugins: {{ legend: {{ display:false }}, tooltip: {{ enabled:false }} }}
      }}
    }});
  </script>
</body>
</html>"""

    return Response(content=html_final, media_type="text/html")
