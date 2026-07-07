// ============================================================
// CONFIGURACIÓN CENTRAL DE CREDENCIALES - DU ACADEMY
// ============================================================
const AC_PHONE_NUMBER_ID   = "1101627349711038";
const AC_ACCESS_TOKEN      = "EAAULe6CV6ZCYBR1DplWkXt11peXSPkhHCi4Xx8KcMMDJ7hs4k61r1aEDEpc46XL35u5ZCRfk6k8YxXDKmnYmZCnGjyvHiLXVLpoNCX6vZCWlZBkk6hERoltww5qQFBvzSFdp0A8fZCAe2DK0ygIIZCJAvZBGurJ1gNar6ZBlwbY5FHwGy02z4SwPRH5LcHgDT4gZDZD"; 
const AC_ACCESS_TOKEN_GEMINI = "AQ.Ab8RN6JI-ALMQZrurk2MJVpIGoF2sROsp-ATv9g2FOS5NzBEkw";

// Conexión a tu Google Sheet
const SS_NUEVO_ID = "152FRbVqwW7MUX3gNP9ZYnq27uJWzQFC_mTHiWRSkZ-o";

// Token idéntico al que debes poner en Meta Developers
const WEBHOOK_VERIFY_TOKEN = "mi_auditoria_segura_2026"; 

// ============================================================
// 🟩 VERIFICACIÓN DEL WEBHOOK: METODO GET (VALIDACIÓN DE META)
// ============================================================
function doGet(e) {
  try {
    const params = e.parameter;
    const mode = params["hub.mode"];
    const token = params["hub.verify_token"];
    const challenge = params["hub.challenge"];

    if (mode === "subscribe" && token === WEBHOOK_VERIFY_TOKEN) {
      Logger.log("✅ Webhook verificado con éxito por Meta.");
      return ContentService.createTextOutput(challenge);
    } else {
      Logger.log("❌ Token de verificación incorrecto en la petición.");
      return ContentService.createTextOutput("Forbidden").setMimeType(ContentService.MimeType.TEXT);
    }
  } catch (err) {
    return ContentService.createTextOutput("Error: " + err.message).setMimeType(ContentService.MimeType.TEXT);
  }
}

// ============================================================
// 🟦 OPERACIÓN DEL WEBHOOK: METODO POST (PROCESADOR DE CHAT)
// ============================================================
function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return ContentService.createTextOutput("No data").setMimeType(ContentService.MimeType.TEXT);
    }

    const body = JSON.parse(e.postData.contents);
    const entrada = body?.entry?.[0]?.changes?.[0]?.value;
    if (!entrada) return respuestaOK_();

    if (entrada?.statuses && !entrada?.messages) return respuestaOK_();

    const msg = entrada?.messages?.[0];
    if (!msg) return respuestaOK_();

    const numero = String(msg.from).trim();

    if (msg.type === "text") {
      const textoUsuario = msg.text?.body || "";
      if (!textoUsuario.trim()) return respuestaOK_();

      marcarEscribiendoWhatsApp_(numero);

      const ss = SpreadsheetApp.openById(SS_NUEVO_ID);
      const hAsesores = ss.getSheetByName("Asesores"); 
      let nombreContacto = "";
      
      if (hAsesores) {
        nombreContacto = obtenerNombrePorNumero_(hAsesores, numero) || "";
      }

      let respuestaFinal = "";

      if (!nombreContacto) {
        respuestaFinal = gestionarNuevoUsuario_(numero, textoUsuario);
      } else {
        respuestaFinal = consultarDuLive_(textoUsuario, nombreContacto);
      }
      
      despacharMensajeWhatsApp_(numero, respuestaFinal);
    }

  } catch (err) {
    Logger.log("❌ Error controlado en doPost: " + err.message);
  }
  return respuestaOK_();
}

// ─── GESTIÓN INTELIGENTE DE NUEVOS CONTACTOS ─────────────────────
function gestionarNuevoUsuario_(numero, textoUsuario) {
  const cache = CacheService.getScriptCache();
  const cacheKey = "esperando_nombre_" + numero;
  const estadoEspera = cache.get(cacheKey);

  if (estadoEspera === "si") {
    const ss = SpreadsheetApp.openById(SS_NUEVO_ID);
    const hAsesores = ss.getSheetByName("Asesores");
    if (hAsesores) {
      const usuarioTemporal = textoUsuario.toUpperCase().replace(/\s+/g, "") + "123";
      hAsesores.appendRow([textoUsuario, usuarioTemporal, numero, "Registrado por Du Academy Bot"]);
    }
    cache.remove(cacheKey);
    return `¡Listo! Ya te guardé en mis contactos como *${textoUsuario}*. ¡Qué alegría tenerte por aquí! 🙌✨ Ahora sí, dime con total confianza, ¿en qué te puedo ayudar hoy? Escríbeme cualquier consulta o pídeme buscar algo en la web. 🧠🚀`;
  } else {
    cache.put(cacheKey, "si", 600);
    return "¡Hola! ¡Qué gusto saludarte! 😊👋 Veo que me escribes desde un número nuevo y no te tengo en mi lista de contactos. ¿Me podrías decir tu nombre completo para registrarte por aquí y saber con quién hablo? ✨";
  }
}

// ─── BUSCADOR EXACTO BASADO EN LA COLUMNA C (WHATSAPP) ───────────
function obtenerNombrePorNumero_(hoja, numeroBuscado) {
  const datos = hoja.getDataRange().getValues();
  const numLimpio = String(numeroBuscado).replace(/\s+/g, "").trim();
  
  for (let i = 1; i < datos.length; i++) {
    const whatsappFila = String(datos[i][2]).replace(/\s+/g, "").trim();
    if (whatsappFila === numLimpio) {
      return datos[i][0];
    }
  }
  return null;
}

// ─── UTILERÍAS DE WHATSAPP API ───────────────────────────────────
function despacharMensajeWhatsApp_(numero, texto) {
  const url = "https://graph.facebook.com/v19.0/" + AC_PHONE_NUMBER_ID + "/messages";
  const payload = {
    messaging_product: "whatsapp",
    to: numero,
    type: "text",
    text: { body: texto }
  };
  const opciones = {
    method: "post",
    contentType: "application/json",
    headers: { "Authorization": "Bearer " + AC_ACCESS_TOKEN },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };
  const resp = UrlFetchApp.fetch(url, opciones);
  Logger.log("CODIGO: " + resp.getResponseCode());
  Logger.log("RESPUESTA: " + resp.getContentText());
}

function marcarEscribiendoWhatsApp_(numero) {
  const url = "https://graph.facebook.com/v19.0/" + AC_PHONE_NUMBER_ID + "/messages";
  const payload = {
    messaging_product: "whatsapp",
    recipient_type: "individual",
    to: numero,
    sender_action: "typing_on"
  };
  const opciones = {
    method: "post",
    contentType: "application/json",
    headers: { "Authorization": "Bearer " + AC_ACCESS_TOKEN },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };
  UrlFetchApp.fetch(url, opciones);
}

function respuestaOK_() {
  return ContentService.createTextOutput(JSON.stringify({ status: "success" }))
                       .setMimeType(ContentService.MimeType.JSON);
}
