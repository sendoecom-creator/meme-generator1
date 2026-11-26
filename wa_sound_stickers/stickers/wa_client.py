import base64
import hashlib
import os
import queue
import shutil
import threading
import time
from django.conf import settings
from playwright.sync_api import sync_playwright

CHROME_PATH = "/usr/bin/google-chrome"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

WA_SEND_SOUND_STICKER_JS = r"""
async ({ chatId, webpB64, oggB64, durationSec, isAnimated }) => {
    const SOUND_PROPS = [
        "sound_stickers_consumption_enabled",
        "sound_stickers_creation_enabled",
        "sound_stickers_audio_playing_enabled",
    ];

    function ns(name) {
        if (typeof window.importNamespace === "function") {
            try { const m = window.importNamespace(name); if (m) return m; } catch (e) {}
        }
        return window.require(name);
    }
    function def(name) {
        const m = ns(name);
        return m && m.default !== undefined ? m.default : m;
    }

    // 1. Force sound sticker gating & ABProps (works even outside beta!)
    const abOriginal = {};
    let gatingMethod = "none";
    try {
        const AB = ns("WAWebABProps");
        for (const p of SOUND_PROPS) {
            try { abOriginal[p] = AB.getABPropConfigValue(p); } catch (e) {}
        }
        const orig = AB.getABPropConfigValue;
        if (!orig.__waas) {
            const patched = function (name) {
                if (SOUND_PROPS.includes(name)) return true;
                return orig.apply(this, arguments);
            };
            patched.__waas = true;
            AB.getABPropConfigValue = patched;
        }
        gatingMethod = "WAWebABProps";
    } catch (e) {}

    let gatingOk = false;
    try {
        const G = ns("WAWebSoundStickerGating");
        if (G) {
            G.isSoundStickerConsumptionEnabled = () => true;
            G.isSoundStickerCreationEnabled = () => true;
            G.isSoundStickerPlaybackEnabled = () => true;
            gatingOk = true;
            gatingMethod += "+WAWebSoundStickerGating";
        }
    } catch (e) {}

    // 2. Resolve target chat (self chat if chatId === "__SELF__")
    const CC = ns("WAWebChatCollection").ChatCollection;
    let chat = null;
    if (!chatId || chatId === "__SELF__") {
        const Me = ns("WAWebUserPrefsMeUser");
        let firstWid = null;
        for (const g of ["getMaybeMePnUser", "getMeUserOrThrow", "getMaybeMeLidUser"]) {
            try {
                const wid = Me[g] && Me[g]();
                if (!wid) continue;
                firstWid = firstWid || wid;
                chat = CC.get(wid) || CC.get(String(wid._serialized || wid));
                if (chat) break;
            } catch (e) {}
        }
        if (!chat && firstWid) {
            const r = await ns("WAWebFindChatAction").findOrCreateLatestChat(firstWid, "forwardSelectedModals");
            chat = r && (r.chat || r);
        }
    } else if (/^\d{7,16}$/.test(chatId.replace(/\D/g, "")) && !chatId.includes("@")) {
        const digits = chatId.replace(/\D/g, "");
        const widStr = `${digits}@c.us`;
        let widObj = widStr;
        try {
            const WF = ns("WAWebWidFactory");
            if (WF && WF.createWid) widObj = WF.createWid(widStr);
        } catch (e) {}
        chat = CC.get(widObj) || CC.get(widStr);
        if (!chat) {
            const r = await ns("WAWebFindChatAction").findOrCreateLatestChat(widObj, "forwardSelectedModals");
            chat = r && (r.chat || r);
        }
    } else {
        chat = CC.get(chatId);
        if (!chat) {
            try {
                const WF = ns("WAWebWidFactory");
                if (WF && WF.createWid) {
                    const widObj = WF.createWid(chatId);
                    chat = CC.get(widObj);
                    if (!chat) {
                        const r = await ns("WAWebFindChatAction").findOrCreateLatestChat(widObj, "forwardSelectedModals");
                        chat = r && (r.chat || r);
                    }
                }
            } catch (e) {}
        }
    }

    if (!chat) {
        throw new Error("Target chat not found. Try selecting a chat or 'My Self'.");
    }
    if (chat.isReadOnly || chat.canSend === false) {
        throw new Error("Cannot send messages to this read-only chat.");
    }

    // 3. Helper: base64 -> Uint8Array / Blob
    function b64ToBytes(b64) {
        const bin = atob(b64);
        const arr = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        return arr;
    }
    function toB64(v) {
        if (v == null || typeof v === "string") return v;
        const bytes = v instanceof ArrayBuffer ? new Uint8Array(v) : new Uint8Array(v.buffer || v);
        let s = "";
        for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
        return btoa(s);
    }
    async function sha256B64(bytes) {
        const digest = await crypto.subtle.digest("SHA-256", bytes);
        return toB64(digest);
    }

    const oggBytes = b64ToBytes(oggB64);
    const oggBlob = new Blob([oggBytes], { type: "audio/ogg; codecs=opus" });
    const webpBytes = b64ToBytes(webpB64);
    const webpBlob = new Blob([webpBytes], { type: "image/webp" });

    // 4. Upload OGG audio for stickerAudio
    const MT = ns("WAWebMmsMediaTypes").MEDIA_TYPES;
    let uploadOrigin = 1;
    try { uploadOrigin = def("WAWebMediaGetUploadOriginForChat")(chat); } catch (e) {}
    const noopQpl = new Proxy({}, { get: () => () => undefined });
    const params = {
        blob: oggBlob,
        type: MT.AUDIO,
        signal: new AbortController().signal,
        uploadQpl: noopQpl,
        uploadOrigin,
        fileOrigin: undefined,
        userUploadAttemptCount: 0,
        forwardedFromWeb: false,
        isViewOnce: false,
        isHdPhoto: false,
    };

    let uploadRes;
    try {
        const metrics = def("WAWebCreateMediaUploadMetrics")(params);
        uploadRes = await def("WAWebUploadManagerMainThread").encryptAndUpload(params, metrics);
    } catch (e) {
        uploadRes = await def("WAWebUploadManager").encryptAndUpload(params);
    }

    const filehash = await sha256B64(oggBytes);
    const stickerAudio = {
        deprecatedMms3Url: uploadRes.url || undefined,
        directPath: uploadRes.directPath,
        mediaKey: toB64(uploadRes.mediaKey),
        mediaKeyTimestamp: uploadRes.mediaKeyTimestamp,
        filehash,
        encFilehash: toB64(uploadRes.encFilehash),
        size: oggBytes.length,
        mimetype: "audio/ogg; codecs=opus",
        duration: durationSec,
    };

    let isComplete = null;
    try {
        isComplete = ns("WAWebSoundStickerUtils").isCompleteStickerAudio(stickerAudio);
    } catch (e) {
        isComplete = "error:" + e.message;
    }
    if (isComplete !== true) {
        throw new Error("Audio validation failed (isCompleteStickerAudio=" + isComplete + ")");
    }

    // 5. Send Sticker with stickerAudio payload
    const Opaque = def("WAWebMediaOpaqueData");
    const MediaPrep = def("WAWebMediaPrep");
    const opaque = await Opaque.createFromData(webpBlob, "image/webp");
    const data = {
        type: "sticker",
        mediaBlob: opaque,
        mimetype: "image/webp",
        size: webpBytes.length,
        fullWidth: 512,
        fullHeight: 512,
        isAnimated: !!isAnimated,
        emojis: ["🔊"],
    };
    const prep = new MediaPrep("sticker", Promise.resolve(data));
    const sendRes = await prep.sendToChat({ chat, options: { stickerAudio } });
    const OK = ns("WAWebSendMsgResultAction").SendMsgResult.OK;
    if (!sendRes || sendRes.messageSendResult !== OK) {
        throw new Error("Send failed: " + (sendRes ? sendRes.messageSendResult : "unknown"));
    }

    // 6. Wait briefly for server ACK
    const msg = sendRes.msg;
    let ack = msg ? msg.ack : null;
    try {
        const ACK = ns("WAWebAck").ACK;
        const t0 = Date.now();
        while (msg && Date.now() - t0 < 15000) {
            ack = msg.ack;
            if (ack >= ACK.SENT) break;
            if (ack < 0) throw new Error("Message error ack: " + ack);
            await new Promise((r) => setTimeout(r, 300));
        }
    } catch (e) {}

    return {
        ok: true,
        chatId: String(chat.id._serialized || chat.id),
        chatName: chat.formattedTitle || chat.name || "Chat",
        msgId: msg && msg.id ? String(msg.id._serialized || msg.id) : null,
        hasStickerAudio: !!(msg && (msg.stickerAudio || (msg.attributes && msg.attributes.stickerAudio))),
        ack,
        gatingMethod,
        gatingOk,
        abOriginal,
    };
}
"""


class WhatsAppClient:
    def __init__(self, session_dir: str):
        self.session_dir = session_dir
        self._cmd_queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started = False
        self.last_active = time.time()

    def _ensure_started(self):
        with self._lock:
            if not self._started:
                self._started = True
                self._thread = threading.Thread(target=self._worker_loop, daemon=True)
                self._thread.start()

    def _run_task(self, fn, timeout=60):
        self.last_active = time.time()
        self._ensure_started()
        res_q: queue.Queue = queue.Queue(maxsize=1)
        self._cmd_queue.put((fn, res_q))
        ok, val = res_q.get(timeout=timeout)
        if not ok:
            raise val
        return val

    def _worker_loop(self):
        session_dir = self.session_dir
        os.makedirs(session_dir, exist_ok=True)
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lp = os.path.join(session_dir, lock_name)
            if os.path.islink(lp) or os.path.exists(lp):
                try:
                    os.unlink(lp)
                except OSError:
                    pass

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=session_dir,
                executable_path=CHROME_PATH,
                headless=True,
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 860},
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60000)

            while True:
                fn, res_q = self._cmd_queue.get()
                try:
                    if fn == "__CLOSE__":
                        try:
                            context.close()
                        except Exception:
                            pass
                        shutil.rmtree(session_dir, ignore_errors=True)
                        res_q.put((True, {"ok": True}))
                        return
                    elif fn == "__RESET__":
                        context.close()
                        shutil.rmtree(session_dir, ignore_errors=True)
                        os.makedirs(session_dir, exist_ok=True)
                        context = p.chromium.launch_persistent_context(
                            user_data_dir=session_dir,
                            executable_path=CHROME_PATH,
                            headless=True,
                            user_agent=USER_AGENT,
                            viewport={"width": 1280, "height": 860},
                            args=[
                                "--no-sandbox",
                                "--disable-dev-shm-usage",
                                "--disable-blink-features=AutomationControlled",
                            ],
                        )
                        page = context.pages[0] if context.pages else context.new_page()
                        page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60000)
                        res_q.put((True, {"ok": True}))
                    else:
                        result = fn(page)
                        res_q.put((True, result))
                except Exception as exc:
                    res_q.put((False, exc))

    def get_status(self) -> dict:
        def _check(page):
            info = page.evaluate("""() => {
                const hasPane = !!document.querySelector('#pane-side');
                const qrCanvas = document.querySelector('canvas[aria-label*="Scan"], div[data-ref] canvas');
                let loggedIn = false;
                let me = null;
                let abOriginal = null;
                try {
                    if (typeof window.require === 'function') {
                        const Me = window.require('WAWebUserPrefsMeUser');
                        const wid = Me.getMaybeMePnUser && Me.getMaybeMePnUser();
                        if (wid) {
                            loggedIn = true;
                            me = String(wid.user || wid._serialized || wid);
                        }
                        const AB = window.require('WAWebABProps');
                        abOriginal = {
                            consumption: AB.getABPropConfigValue('sound_stickers_consumption_enabled'),
                            creation: AB.getABPropConfigValue('sound_stickers_creation_enabled'),
                            playing: AB.getABPropConfigValue('sound_stickers_audio_playing_enabled'),
                        };
                    }
                } catch (e) {}
                return {
                    ready: hasPane || loggedIn,
                    hasQr: !!qrCanvas,
                    me,
                    abOriginal,
                };
            }""")

            if info.get("ready"):
                return {
                    "status": "ready",
                    "me": info.get("me"),
                    "ab_original": info.get("abOriginal"),
                }

            if info.get("hasQr"):
                qr_el = page.locator('div[data-ref]').first
                if qr_el.count() == 0:
                    qr_el = page.locator('canvas[aria-label*="Scan"]').first
                png_bytes = qr_el.screenshot()
                return {
                    "status": "qr",
                    "qr_base64": base64.b64encode(png_bytes).decode("ascii"),
                }

            # Check if currently showing pairing code
            link_code = page.evaluate("""() => {
                const el = document.querySelector('[data-link-code]');
                return el ? el.getAttribute('data-link-code') : null;
            }""")
            if link_code:
                clean_code = link_code.replace(",", "").strip()
                formatted = f"{clean_code[:4]}-{clean_code[4:]}" if len(clean_code) == 8 else clean_code
                return {"status": "pairing", "pairing_code": formatted}

            return {"status": "loading"}

        return self._run_task(_check, timeout=30)

    def request_pairing_code(self, phone_number: str) -> dict:
        digits = "".join(ch for ch in (phone_number or "") if ch.isdigit())
        if len(digits) < 8:
            raise ValueError("Please enter a valid phone number with country code (e.g. +919895061390).")
        full_phone = "+" + digits

        def _pair(page):
            # If already on pairing code screen or phone input screen, go back to QR or use input directly
            inp = page.locator('[data-testid="phone-number-input"]').first
            if inp.count() == 0:
                # If on pairing code screen, click "Link with QR code" or reload
                if page.locator('[data-link-code]').count() > 0:
                    page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector('canvas[aria-label*="Scan"], [data-testid="link_device_qr_phone_number_link"]', timeout=30000)
                time.sleep(0.8)
                btn = page.locator('[data-testid="link_device_qr_phone_number_link"]').first
                btn.scroll_into_view_if_needed()
                btn.click()
                inp = page.locator('[data-testid="phone-number-input"]').first
                inp.wait_for(timeout=15000)

            inp.click()
            page.keyboard.press("Control+a")
            page.keyboard.type(full_phone, delay=25)
            time.sleep(0.4)
            page.locator('button:has-text("Next")').first.click()

            # Wait for either pairing code or validation error
            for _ in range(40):
                code = page.evaluate("""() => {
                    const el = document.querySelector('[data-link-code]');
                    return el ? el.getAttribute('data-link-code') : null;
                }""")
                if code:
                    clean_code = code.replace(",", "").strip()
                    formatted = f"{clean_code[:4]}-{clean_code[4:]}" if len(clean_code) == 8 else clean_code
                    return {"pairing_code": formatted}
                err_txt = page.evaluate("""() => {
                    const txt = document.body.innerText || '';
                    if (txt.includes('A valid phone number is required')) return 'Invalid phone number. Include country code (e.g. +91...).';
                    if (txt.includes('Couldn\'t link device') || txt.includes('Try again')) return 'WhatsApp rejected the request. Please wait a moment and try again.';
                    return null;
                }""")
                if err_txt:
                    raise ValueError(err_txt)
                time.sleep(0.5)

            raise TimeoutError("Timed out waiting for WhatsApp pairing code.")

        return self._run_task(_pair, timeout=45)

    def get_chats(self) -> list[dict]:
        def _chats(page):
            return page.evaluate("""() => {
                const CC = window.require('WAWebChatCollection').ChatCollection;
                const models = CC.getModelsArray ? CC.getModelsArray() : [];
                const sorted = models.slice().sort((a, b) => (b.t || 0) - (a.t || 0));
                return sorted.slice(0, 40).map(c => ({
                    id: String(c.id._serialized || c.id),
                    name: c.formattedTitle || c.name || (c.id && c.id.user) || 'Unknown',
                    isGroup: !!(c.id && c.id.isGroup && c.id.isGroup()),
                    isReadOnly: !!(c.isReadOnly || c.canSend === false),
                }));
            }""")

        return self._run_task(_chats, timeout=20)

    def send_voice_sticker(
        self,
        chat_id: str,
        webp_bytes: bytes,
        ogg_bytes: bytes,
        duration_sec: int,
        is_animated: bool,
    ) -> dict:
        payload = {
            "chatId": chat_id,
            "webpB64": base64.b64encode(webp_bytes).decode("ascii"),
            "oggB64": base64.b64encode(ogg_bytes).decode("ascii"),
            "durationSec": int(duration_sec),
            "isAnimated": bool(is_animated),
        }

        def _send(page):
            return page.evaluate(WA_SEND_SOUND_STICKER_JS, payload)

        return self._run_task(_send, timeout=60)

    def logout(self) -> dict:
        return self._run_task("__RESET__", timeout=45)

    def close_and_wipe(self) -> None:
        if self._started:
            try:
                self._run_task("__CLOSE__", timeout=15)
            except Exception:
                shutil.rmtree(self.session_dir, ignore_errors=True)


class WhatsAppSessionManager:
    """Strictly isolates each visitor's WhatsApp Web session and wipes idle sessions."""

    IDLE_TTL_SECONDS = 2 * 3600

    def __init__(self):
        self._clients: dict[str, WhatsAppClient] = {}
        self._lock = threading.Lock()

    def _safe_dir(self, sid: str) -> str:
        digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:24]
        return os.path.join(str(settings.WA_SESSIONS_ROOT), digest)

    def get_client(self, sid: str) -> WhatsAppClient:
        now = time.time()
        expired: list[tuple[str, WhatsAppClient]] = []
        with self._lock:
            for k, cl in list(self._clients.items()):
                if k != sid and (now - cl.last_active) > self.IDLE_TTL_SECONDS:
                    expired.append((k, self._clients.pop(k)))
            if sid not in self._clients:
                self._clients[sid] = WhatsAppClient(self._safe_dir(sid))
            client = self._clients[sid]
            client.last_active = now

        for _, cl in expired:
            cl.close_and_wipe()
        return client

    def destroy_client(self, sid: str) -> None:
        with self._lock:
            cl = self._clients.pop(sid, None)
        if cl:
            cl.close_and_wipe()
        else:
            shutil.rmtree(self._safe_dir(sid), ignore_errors=True)


wa_manager = WhatsAppSessionManager()
