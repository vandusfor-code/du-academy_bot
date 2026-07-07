import os
import re
import unicodedata
import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()

AC_PHONE_NUMBER_ID = os.environ["AC_PHONE_NUMBER_ID"]
AC_ACCESS_TOKEN = os.environ["AC_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
WEBHOOK_VERIFY_TOKEN = os.environ["WEBHOOK_VERIFY_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

WA_API_VERSION = "v20.0"
GEMINI_MODEL = "gemini-2.5-flash"

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
    nombre_asesora = await buscar_asesora(numero)
    if not nombre_asesora:
        respuesta = await gestionar_nuevo_usuario(numero, texto)
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


async def buscar_asesora(numero: str):
    filas = await _sb_get("asesoras", {
        "or": f"(numero.eq.{numero},numero_alt.eq.{numero})",
        "select": "nombre",
        "limit": "1",
    })
    return filas[0]["nombre"] if filas else None


async def gestionar_nuevo_usuario(numero: str, texto: str) -> str:
    pendientes = await _sb_get("registro_pendiente", {"numero": f"eq.{numero}", "select": "numero"})
    if pendientes:
        nombre = texto.strip()
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


# ============================================================
# CEREBRO DE DU — 1 sola llamada a Gemini con prioridad de fuentes + memoria
# ============================================================

async def consultar_du_bot(mensaje_usuario: str, nombre_asesora: str, numero: str) -> str:
    historial = await obtener_historial(numero)
    archivos_parts = await obtener_manuales(mensaje_usuario)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

    system_instruction = (
        "Tu nombre es Du. Eres el compañero de trabajo virtual de las asesoras de COFREM en People BPO, "
        "ayudándolas en tiempo real mientras atienden llamadas o chats.\n\n"
        f"Te habla: *{nombre_asesora}*, tu compañera de equipo. Trátala con calidez y compañerismo, sin formalidad excesiva.\n\n"
        "ORDEN DE PRIORIDAD DE FUENTES (decide tú misma cuál usar, sin avisar el proceso):\n"
        "1. Primero revisa los documentos PDF adjuntos (manuales internos oficiales). Si el dato está ahí, respóndelo basado en eso.\n"
        "2. Si no está en los documentos, usa la búsqueda web pero confía SOLO en resultados de cofrem.com.co.\n"
        "3. Si tampoco encuentras nada ahí, puedes hacer una búsqueda más general, pero solo sobre temas relacionados con "
        "cajas de compensación familiar / Cofrem, y aclara en tu respuesta que es información general no oficial y que debe validarse.\n"
        "4. Si de verdad no encuentras nada en ninguna fuente, dilo honestamente: no inventes ni asumas.\n\n"
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
        "- NUNCA anuncies \"voy a buscar en tal parte\" ni menciones tu proceso interno de búsqueda: responde directo con la información."
    )

    contenido_historial = [{"role": h["rol"], "parts": [{"text": h["texto"]}]} for h in historial]
    contents = contenido_historial + [{"role": "user", "parts": archivos_parts + [{"text": mensaje_usuario}]}]

    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 800},
    }

    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload, timeout=45.0)
            if res.status_code != 200:
                print(f"❌ Error Gemini: {res.text}")
                return f"Oye {nombre_asesora}, tuve un problema procesando tu consulta. ¿Puedes intentar de nuevo? 🛠️✨"

            data = res.json()
            candidates = data.get("candidates")
            if candidates and candidates[0].get("content", {}).get("parts"):
                texto_respuesta = candidates[0]["content"]["parts"][0].get("text", "")
            else:
                texto_respuesta = f"Oye {nombre_asesora}, se me cortó la señal un segundo. ¿Me repites la pregunta? ⚡💜"

            await guardar_historial(numero, mensaje_usuario, texto_respuesta)
            return texto_respuesta
        except Exception as e:
            print(f"❌ Error en consultar_du_bot: {e}")
            return f"Hola {nombre_asesora}. Tuve un contratiempo de conexión. Dame un minutito e intenta de nuevo. 🛠️✨"


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
