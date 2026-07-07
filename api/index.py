import os
import httpx
from fastapi import FastAPI, Request, Response, BackgroundTasks

app = FastAPI()

# ============================================================
# CONFIGURACIÓN CENTRAL DE CREDENCIALES - DU ACADEMY
# ============================================================
AC_PHONE_NUMBER_ID = "1101627349711038"
AC_ACCESS_TOKEN = "EAAULe6CV6ZCYBR1DplWkXt11peXSPkhHCi4Xx8KcMMDJ7hs4k61r1aEDEpc46XL35u5ZCRfk6k8YxXDKmnYmZCnGjyvHiLXVLpoNCX6vZCWlZBkk6hERoltww5qQFBvzSFdp0A8fZCAe2DK0ygIIZCJAvZBGurJ1gNar6ZBlwbY5FHwGy02z4SwPRH5LcHgDT4gZDZD"
AC_ACCESS_TOKEN_GEMINI = "AQ.Ab8RN6JI-ALMQZrurk2MJVpIGoF2sROsp-ATv9g2FOS5NzBEkw"
WEBHOOK_VERIFY_TOKEN = "mi_auditoria_segura_2026"

cache_msg_ids = set()

@app.get("/api")
def verificar_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == WEBHOOK_VERIFY_TOKEN:
        return Response(content=params.get("hub.challenge"), media_type="text/plain")
    return Response(content="Forbidden", status_code=403)

@app.post("/api")
async def recibir_mensaje(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
        
        if not body or "entry" not in body or not body["entry"]:
            return Response(content='{"status":"no entry"}', media_type="application/json")
            
        entry = body["entry"][0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        
        if "statuses" in value and "messages" not in value:
            return Response(content='{"status":"status update"}', media_type="application/json")
            
        messages = value.get("messages", [])
        if not messages:
            return Response(content='{"status":"no messages"}', media_type="application/json")
            
        msg = messages[0]
        msg_id = msg.get("id")
        
        if msg_id in cache_msg_ids:
            return Response(content='{"status":"duplicate ignored"}', media_type="application/json")
        cache_msg_ids.add(msg_id)
        
        if msg.get("type") == "text":
            numero = str(msg.get("from")).strip()
            texto_usuario = msg.get("text", {}).get("body", "").strip()
            
            if not texto_usuario:
                return Response(content='{"status":"empty text"}', media_type="application/json")
                
            background_tasks.add_task(procesar_flujo_bot, numero, texto_usuario)
            
    except Exception as e:
        print(f"❌ Error doPost FastAPI: {str(e)}")
        
    return Response(content='{"status":"success"}', media_type="application/json")

async def procesar_flujo_bot(numero: str, texto: str):
    nombre_contacto = "Duvan"  # Nombre harcodeado temporal para pruebas de respuesta directa
    await marcar_escribiendo_whatsapp(numero)
    respuesta = await consultar_du_live(texto, nombre_contacto)
    await despachar_mensaje_whatsapp(numero, respuesta)

# ============================================================
# MÓDULO DU LIVE: ASISTENTE GENERAL GRATUITO CON BÚSQUEDA WEB
# ============================================================
async def consultar_du_live(mensaje_usuario: str, nombre_contacto: str) -> str:
    url = "https://googleapis.com" + AC_ACCESS_TOKEN_GEMINI
    
    system_instruction = (
        "Tu nombre es Du. Eres un asistente virtual inteligente y el segundo cerebro del usuario en WhatsApp.\n"
        f"Te está hablando directamente: *{nombre_contacto}*. Trátale con máxima cercanía, amabilidad y empatía, como un colega de confianza.\n\n"
        "REGLAS DE RESPUESTA:\n"
        "1. Tono Humano y Fluido: Habla de forma natural, fresca y servicial. Apóyate en emojis (✨, 🙌, 🧠, 🚀, 💬).\n"
        "2. Formato Limpio WhatsApp: Sé directo y estructurado. Si la respuesta es extensa, organiza la información usando viñetas o listas numeradas fáciles de leer en una pantalla móvil.\n"
        "3. Búsqueda Web Activa (Google Search Grounding): Tienes acceso ilimitado a internet en tiempo real. Si " + nombre_contacto + " te pregunta sobre actualidad, direcciones, clima, eventos o consultas técnicas, búscalo en Google y entrégale la información masticada y resumida.\n"
        f"4. Respuestas Cortas de Cortesía: Si te saluda o agradece de forma breve (ej: 'gracias', 'ok', 'listo', 'bueno'), responde con un mensaje de cierre corto y animado (ej: '¡Con gusto, {nombre_contacto}! Aquí sigo pendiente si necesitas algo más', '¡De una! Quedo atento.')."
    )
    
    payload = {
        "contents": [{"parts": [{"text": mensaje_usuario}]}],
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "tools": [{"googleSearch": {}}],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 1024
        }
    }
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload, timeout=30.0)
            if res.status_code == 200:
                data = res.json()
                if "candidates" in data and data["candidates"]:
                    candidate = data["candidates"][0]
                    if "content" in candidate and "parts" in candidate["content"]:
                        parts = candidate["content"]["parts"][0]
                        if "text" in parts:
                            return parts["text"]
                            
                return f"Oye {nombre_contacto}, se me cortó la señal un segundo procesando la info. ¿Me repites la pregunta? ⚡💜"
            else:
                return f"Hola {nombre_contacto}. Tuve un pequeño contratiempo de conexión con mis servidores. Dame un minutito e intenta de nuevo. 🛠️✨"
        except Exception as e:
            return f"Lo siento {nombre_contacto}, se generó un error interno al procesar tu mensaje. ⚙️"

async def despachar_mensaje_whatsapp(numero: str, texto: str):
    url = f"https://facebook.com{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": texto}
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload, headers=headers)

async def marcar_escribiendo_whatsapp(numero: str):
    url = f"https://facebook.com{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "sender_action": "typing"
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload, headers=headers)
