import os
import httpx
from fastapi import FastAPI, Request, Response, BackgroundTasks

app = FastAPI()

# ─── CONFIGURACIÓN DE CREDENCIALES ───
AC_PHONE_NUMBER_ID = "1101627349711038"
AC_ACCESS_TOKEN = "EAAULe6CV6ZCYBR1DplWkXt11peXSPkhHCi4Xx8KcMMDJ7hs4k61r1aEDEpc46XL35u5ZCRfk6k8YxXDKmnYmZCnGjyvHiLXVLpoNCX6vZCWlZBkk6hERoltww5qQFBvzSFdp0A8fZCAe2DK0ygIIZCJAvZBGurJ1gNar6ZBlwbY5FHwGy02z4SwPRH5LcHgDT4gZDZD"
WEBHOOK_VERIFY_TOKEN = "cofrem_du_bot_2026"
GEMINI_API_KEY = "AQ.Ab8RN6JI-ALMQZrurk2MJVpIGoF2sROsp-ATv9g2FOS5NzBEkw"

# Memoria temporal en el servidor
cache_msg_ids = set()
historial_conversaciones = {}

@app.get("/webhook")
def verificar_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == WEBHOOK_VERIFY_TOKEN:
        return Response(content=params.get("hub.challenge"), media_type="text/plain")
    return Response(content="Forbidden", status_code=403)

@app.post("/webhook")
async def recibir_mensaje(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
        
        if not body or "entry" not in body or not body["entry"]:
            return Response(content='{"status":"no entry"}', media_type="application/json")
            
        entry = body["entry"][0]
        if "changes" not in entry or not entry["changes"]:
            return Response(content='{"status":"no changes"}', media_type="application/json")
            
        value = entry["changes"][0].get("value", {})
        
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
    nombre_asesora = "Duvan"
    await marcar_escribiendo_whatsapp(numero)
    respuesta = await consultar_du_bot(texto, nombre_asesora, numero)
    await despachar_whatsapp(numero, respuesta)

async def consultar_du_bot(mensaje_usuario: str, nombre_asesora: str, numero: str) -> str:
    url = f"https://googleapis.com{GEMINI_API_KEY}"
    
    system_instruction = (
        "Tu nombre es Du. Eres el compañero de trabajo virtual de las asesoras de COFREM en People BPO, ayudándolas en tiempo real.\n\n"
        f"Te habla: *{nombre_asesora}*. Trátala con calidez y compañerismo.\n\n"
        "ORDEN DE PRIORIDAD:\n"
        "1. Revisa los documentos PDF adjuntos (manuales).\n"
        "2. Si no está, usa búsqueda web confiando SOLO en cofrem.com.co.\n"
        "3. Si no, usa información general aclarando que no es oficial.\n\n"
        "REGLA DE ORO - BREVEDAD: Responde en máximo 3-4 líneas o 3 puntos clave.\n"
        "FORMATO WHATSAPP: Negrita con un asterisco *así*. Listas cortas con guiones (-). Sin Markdown (#).\n"
        "NUNCA menciones tu proceso interno de búsqueda ni digas 'voy a buscar'. Responde directo con la solución."
    )
    
    historial = historial_conversaciones.get(numero, [])
    contents = list(historial)
    contents.append({"role": "user", "parts": [{"text": mensaje_usuario}]})
    
    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "tools": [{"googleSearch": {}}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 800}
    }
    
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload, timeout=30.0)
            if res.status_code == 200:
                data = res.json()
                if "candidates" in data and data["candidates"]:
                    candidate = data["candidates"][0]
                    if "content" in candidate and "parts" in candidate["content"]:
                        parts = candidate["content"]["parts"]
                        if parts and "text" in parts[0]:
                            texto_res = parts[0]["text"]
                            
                            historial.append({"role": "user", "parts": [{"text": mensaje_usuario}]})
                            historial.append({"role": "model", "parts": [{"text": texto_res}]})
                            historial_conversaciones[numero] = historial[-10:]
                            return texto_res
                        
                return f"Oye {nombre_asesora}, se me cruzaron los cables con el formato. ¿Me repites? ⚡"
            else:
                print(f"❌ Error Gemini Status {res.status_code}: {res.text}")
                return f"Oye {nombre_asesora}, tuve un problema con el cerebro de datos. ¿Intentas de nuevo? 🛠️"
        except Exception as e:
            print(f"❌ Excepción Gemini: {str(e)}")
            return f"Lo siento {nombre_asesora}, se generó un error interno al procesar tu mensaje. ⚙️"

async def despachar_whatsapp(numero: str, texto: str):
    # ✅ CORREGIDO: ://facebook.com bien estructurado
    url = f"https://://facebook.com/v19.0/{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "type": "text",
        "text": {"body": texto}
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload, headers=headers)

async def marcar_escribiendo_whatsapp(numero: str):
    # ✅ CORREGIDO: ://facebook.com bien estructurado
    url = f"https://://facebook.com/v19.0/{AC_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {AC_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero,
        "sender_action": "typing"
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload, headers=headers)
