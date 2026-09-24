"""
מודול תמלול-קול גנרי לשלוחות ימות המשיח (type=api)
====================================================
מודול "Vercel Python" עצמאי, שכל מערכת ימות יכולה לחבר אליו שלוחת
type=api ולקבל: המאזין נכנס לשלוחה -> מתבקש להקליט -> ההקלטה מתומללת
-> הטקסט המתומלל נכתב כקובץ TTS בנתיב שהוגדר מראש (בהגדרות השלוחה
עצמה, ב-ext.ini) -> אפשר לנתב את השיחה הלאה כדי שתשמע/תמשיך.

--------------------------------------------------------------------
זרימת השיחה (call flow) הנתמכת
--------------------------------------------------------------------
1. שלוחה מסוג type=api עם api_link שמצביע לכתובת של המודול הזה
   (.../api/transcribe). ההגדרות של יעד השמירה מגיעות מפרמטרים
   קבועים שמוגדרים ב-ext.ini של אותה שלוחה (api_add_0, api_add_1,...),
   ולא נשלחים מהמאזין - כך שכל שלוחה בכל מערכת יכולה להצביע ליעד שונה.

2. קריאה ראשונה (אין עדיין הקלטה, data מכיל רק את פרטי השיחה):
   המודול מחזיר תגובת "read" מסוג הקלטה (record) שמבקשת מהמאזין
   להקליט הודעה.

3. קריאה שנייה (אחרי שהמאזין סיים להקליט): ימות שולח את בקשת ה-API
   השנייה כאשר ההקלטה כבר נשמרה בשלוחה עצמה (לא בתוך גוף הבקשה!
   type=api של ימות לא שולח בייטים גולמיים של קובץ - הוא רק מיידע
   שיש הקלטה בנתיב מוגדר). המודול:
     א. מוריד את קובץ ה-wav שהמאזין הקליט, דרך פקודת הניהול
        DownloadFile (עם ה-Token שהוגדר לשלוחה זו).
     ב. מריפוד קלים (0.5 שניות שקט לפני/אחרי) ושולח לתמלול Google
        Web Speech API (כמו בקוד המקורי).
     ג. כותב את הטקסט המתומלל כקובץ TTS (UTF-8, סיומת .tts) בנתיב
        היעד המוגדר, בעזרת UploadFile.
     ד. מחזיר תגובת ניתוב (go_to_folder / hangup / הודעת סיום לפי
        בחירה) כדי שהשיחה תמשיך.

--------------------------------------------------------------------
הגדרות בקובץ ext.ini של השלוחה (type=api)
--------------------------------------------------------------------
type=api
api_link=https://<your-vercel-app>.vercel.app/api/transcribe

# --- טוקן הניהול של המערכת (system:password) - להורדת ההקלטה ולהעלאת ה-TTS ---
api_add_0=token=0700000000:MyPassword123

# --- נתיב שמירת קובץ ה-TTS עם התוצאה (ללא סיומת - המודול יוסיף .tts) ---
api_add_1=tts_path=ivr2:/4/M9

# --- (רשות) שלוחת יעד לניתוב אחרי סיום התמלול, ברירת מחדל: המשך לקובץ הבא ---
api_add_2=next_folder=/4

# הערה: קובץ ההקלטה וקובץ ה-TTS נקבעים אוטומטית (000, 001, 002...) -
# אין צורך יותר להגדיר record_file או tts_path.

לפרטים מלאים ראו README.md שבחבילה זו.

--------------------------------------------------------------------
תלות בקוד המקורי (transcribe.py) ומה השתנה
--------------------------------------------------------------------
* שיטת התמלול, שפת התמלול (he-IL) וזמן ריפוד השקט (0.5 שניות) הועתקו
  כלשונם מהקובץ המקורי transcribe.py. עצם הריפוד מומש מחדש עם מודול
  ה-wave המובנה של Python (במקום pydub+ffmpeg), כי סביבת Vercel
  Python הרגילה לא כוללת בינארי ffmpeg - ראו הערה בפונקציה
  pad_with_silence.
* המקור היה שירות "טיפש" שרק מקבל בייטי wav גולמיים ומחזיר טקסט,
  והלוגיקה של הורדה מימות/כתיבת TTS הייתה בצד ה-Node.js הראשי.
  כאן כל הלוגיקה הזו הוכנסה למודול הזה עצמו, כדי שהוא יהיה שלם
  ועצמאי ויתחבר ישירות לשלוחת type=api בלי שרת Node.js נפרד.
* נוספה תמיכה במולטי-מערכת: כל שלוחה מעבירה את הטוקן ואת נתיב
  היעד שלה בהגדרות (api_add_X), כך שאותו deployment אחד ב-Vercel
  יכול לשרת כל מספר מערכות/שלוחות ימות במקביל.
"""

import io
import json
import os
import time
import urllib.parse
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler

import speech_recognition as sr

# ==================== קבועים (מהקוד המקורי) ====================

TRANSCRIBE_LANGUAGE = 'he-IL'          # שפת התמלול. אפשר לשנות ל-en-US וכו'.
SILENCE_PADDING_MS = 500               # ריפוד שקט לפני/אחרי ההקלטה (מ"ש)

YEMOT_API_BASE = 'https://www.call2all.co.il/ym/api/'
DEFAULT_NEXT_ACTION = None             # None => הודעת סיום פשוטה + ניתוק


# ==================== שכבת תמלול (זהה במהותה למקור) ====================

def pad_with_silence(wav_bytes: bytes) -> bytes:
    """
    מוסיף שקט לפני ואחרי קובץ wav, ומחזיר בייטים של wav חדש.

    הערה לעומת הקוד המקורי (transcribe.py): שם הריפוד נעשה עם pydub,
    שדורש בינארי ffmpeg מותקן בסביבת הריצה. סביבת ה-Vercel Python
    הסטנדרטית (Serverless Functions) אינה כוללת ffmpeg מובנה, ולכן
    כאן הריפוד נעשה בעזרת מודול ה-wave המובנה של Python בלבד - הוספת
    "שקט דיגיטלי" (בייטים של אפסים, בהתאמה לעומק הביט) ישירות בפריימים
    הגולמיים של קובץ ה-wav. זה עובד היטב לקבצי PCM WAV רגילים (כפי
    שימות שומר הקלטות), ולא דורש שום תלות בינארית חיצונית - חשוב
    מאוד בסביבת serverless שבה אי אפשר להתקין חבילות מערכת.
    """
    with wave.open(io.BytesIO(wav_bytes), 'rb') as src:
        params = src.getparams()
        frames = src.readframes(params.nframes)

    silence_frames = int(params.framerate * SILENCE_PADDING_MS / 1000)
    silence_bytes = b'\x00' * (silence_frames * params.nchannels * params.sampwidth)

    out = io.BytesIO()
    with wave.open(out, 'wb') as dst:
        dst.setparams(params)
        dst.writeframes(silence_bytes + frames + silence_bytes)
    return out.getvalue()


def transcribe_wav_bytes(wav_bytes: bytes) -> str:
    """מתמלל בייטי wav לטקסט. מחזיר מחרוזת ריקה אם לא זוהה דיבור."""
    padded_bytes = pad_with_silence(wav_bytes)

    recognizer = sr.Recognizer()
    with sr.AudioFile(io.BytesIO(padded_bytes)) as source:
        audio_data = recognizer.record(source)

    try:
        return recognizer.recognize_google(audio_data, language=TRANSCRIBE_LANGUAGE)
    except sr.UnknownValueError:
        return ''
    except sr.RequestError as exc:
        raise RuntimeError(f'שירות התמלול של גוגל לא זמין כרגע: {exc}') from exc


# ==================== שכבת אינטגרציה מול ה-Management API של ימות ====================
# מסמך המקור: https://apiforum.yemot.tel/post/447 (רשימת פקודות)
#             https://f2.freeivr.co.il/post/32031 (UploadFile)
# הערה: הפקודות משתמשות ב-multipart/form-data או query params רגילים.
# הטוקן (token) לעולם לא נרשם ללוג ולא מוחזר בתשובת ה-API ללקוח.

class YemotApiError(RuntimeError):
    pass


def _yemot_get(command: str, params: dict) -> dict:
    url = YEMOT_API_BASE + command + '?' + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    if data.get('responseStatus') not in ('OK', None) and 'responseStatus' in data:
        raise YemotApiError(f'{command} נכשל: {data}')
    return data


def normalize_ivr_path(path: str) -> str:
    """מוודא שהנתיב מתחיל ב-ivr2:/ כמצופה ע"י ה-API (ראה CheckIfFileExists בתיעוד)."""
    path = path.strip()
    if path.startswith('ivr2:'):
        return path
    if path.startswith('ivr:'):
        path = path[len('ivr:'):]
    if not path.startswith('/'):
        path = '/' + path
    return 'ivr2:' + path


def check_file_exists(token: str, ivr_path: str) -> bool:
    """בודק אם קובץ קיים בנתיב הנתון, בעזרת פקודת CheckIfFileExists."""
    data = _yemot_get('CheckIfFileExists', {'token': token, 'path': normalize_ivr_path(ivr_path)})
    if data.get('responseStatus') not in ('OK', None):
        raise YemotApiError(f'בדיקת קיום קובץ נכשלה: {data}')
    return bool(data.get('fileExists'))


def find_next_free_number(token: str, folder_ivr_path: str) -> str:
    """
    מוצא את המספר התלת-ספרתי הפנוי הבא (000, 001, 002...) בתיקייה נתונה,
    כך שכל הקלטה/תמלול חדשים מקבלים קובץ משלהם בלי דריסת קודמיו.
    בודק לפי קיום קובץ ה-wav (ההקלטה) בתיקייה, כדי לשמור על זוגיות
    בין מספר ההקלטה למספר קובץ ה-TTS התואם לו.
    """
    n = 0
    while True:
        candidate = f'{n:03d}'
        wav_path = f'{folder_ivr_path}/{candidate}.wav'
        if not check_file_exists(token, wav_path):
            return candidate
        n += 1


def download_recording(token: str, ivr_path: str) -> bytes:
    """מוריד קובץ wav מהשלוחה בעזרת פקודת DownloadFile."""
    url = (
        YEMOT_API_BASE
        + 'DownloadFile?'
        + urllib.parse.urlencode({'token': token, 'path': normalize_ivr_path(ivr_path)})
    )
    with urllib.request.urlopen(url, timeout=20) as resp:
        content_type = resp.headers.get('Content-Type', '')
        body = resp.read()
    # אם ההורדה נכשלת, ימות מחזיר JSON עם שגיאה במקום בייטי wav.
    if 'application/json' in content_type or body[:1] in (b'{', b'['):
        try:
            data = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            data = {'raw': body[:200]}
        raise YemotApiError(f'הורדת ההקלטה נכשלה: {data}')
    return body


def upload_tts_text(token: str, ivr_path_no_ext: str, text: str) -> None:
    """
    מעלה את הטקסט המתומלל כקובץ TTS (UTF-8, סיומת .tts) בעזרת UploadFile.
    ראה מקור לפורמט קובצי TTS: https://f2.freeivr.co.il/post/2902
    ("שומרים בפורמט utf-8 ובסיומת .tts").
    UploadFile דורש multipart/form-data (ראה https://f2.freeivr.co.il/post/32031).
    """
    if not ivr_path_no_ext.lower().endswith('.tts'):
        ivr_path_no_ext = ivr_path_no_ext + '.tts'
    path = normalize_ivr_path(ivr_path_no_ext)

    boundary = f'----yemot-tts-{int(time.time() * 1000)}'
    file_bytes = text.encode('utf-8')

    def field(name, value):
        return (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'
        ).encode('utf-8')

    body = b''
    body += field('token', token)
    body += field('path', path)
    filename = os.path.basename(path)
    body += (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f'Content-Type: text/plain; charset=utf-8\r\n\r\n'
    ).encode('utf-8')
    body += file_bytes
    body += f'\r\n--{boundary}--\r\n'.encode('utf-8')

    req = urllib.request.Request(
        YEMOT_API_BASE + 'UploadFile',
        data=body,
        method='POST',
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    if data.get('responseStatus') not in ('OK', None):
        raise YemotApiError(f'העלאת קובץ ה-TTS נכשלה: {data}')


# ==================== פענוח פרמטרים שמגיעים מימות ====================

def parse_yemot_payload(raw_body: bytes, content_type: str) -> dict:
    """
    ימות שולח את קריאת ה-type=api כ-POST עם body בפורמט
    application/x-www-form-urlencoded (או querystring דומה ב-GET).
    כולל: ApiPhone, ApiDID, ApiCallId, ApiExtension, ApiTime, וכל
    api_add_X שהוגדר בשלוחה (עם השם שאחרי ה-'=' הראשון כמפתח).
    """
    text = raw_body.decode('utf-8', errors='replace')
    parsed = dict(urllib.parse.parse_qsl(text, keep_blank_values=True))
    return parsed


def build_recording_path(extension: str, record_file: str) -> str:
    """
    בונה את הנתיב לקובץ ההקלטה שהמאזין השאיר, בהנחה שהיא נשמרה
    תחת אותה שלוחה (type=api) כקובץ 000.wav (או כפי שהוגדר).
    extension מגיע מ-ApiExtension, לדוגמה '/4'.
    """
    ext = extension if extension.startswith('/') else '/' + extension
    name = record_file if record_file.lower().endswith('.wav') else record_file + '.wav'
    return f'ivr2:{ext}/{name}'


# ==================== תגובות ל-type=api (syntax מתועד) ====================
# ראה: python scripts/search_docs.py "read record go_to_folder id_list_message"

def response_ask_for_recording(record_file: str) -> str:
    """
    מבקש מהמאזין להקליט לתוך קובץ record_file (בשלוחה הנוכחית).
    תחביר read עם סוג record, לפי api-and-integrations.md.
    record_file הוא כעת המספר התלת-ספרתי הפנוי הבא (000, 001, 002...),
    כדי שכל הקלטה תישמר בקובץ נפרד משלה ולא תדרוס הקלטה קודמת.
    """
    return f'read=record-{record_file}=recording'


def response_route_next(next_folder: str | None) -> str:
    if next_folder:
        return f'go_to_folder={next_folder}'
    return 'id_list_message=t-התמלול הושלם, תודה'


def response_error(message_he: str) -> str:
    # לעולם לא לחשוף פרטי שגיאה טכניים/טוקן למאזין - רק הודעה כללית.
    return f'id_list_message=t-{message_he}'


# ==================== ה-Handler עצמו ====================

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self._handle()

    def do_GET(self):
        self._handle()

    def _handle(self):
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            raw_body = self.rfile.read(length) if length else b''

            if self.command == 'GET':
                qs = urllib.parse.urlparse(self.path).query
                params = dict(urllib.parse.parse_qsl(qs, keep_blank_values=True))
            else:
                params = parse_yemot_payload(raw_body, self.headers.get('Content-Type', ''))

            reply = self._route(params)
            self._send_text(200, reply)

        except YemotApiError as exc:
            self._send_text(200, response_error('אירעה תקלה בשמירת התמלול, נסו שוב מאוחר יותר'))
            self._log_error(exc)
        except Exception as exc:  # noqa: BLE001 - כל שגיאה חייבת להחזיר תגובת ימות תקינה
            self._send_text(200, response_error('אירעה שגיאה, אנא נסו שוב'))
            self._log_error(exc)

    def _route(self, params: dict) -> str:
        # --- קריאת ה-token וההגדרות הקבועות שהוגדרו ב-ext.ini של השלוחה ---
        # שימו לב: record_file ו-tts_path כבר לא נדרשים כהגדרה - המודול
        # קובע אותם אוטומטית (000, 001, 002...) לפי ההקלטה/תמלול הבא הפנוי.
        token = params.get('token', '')
        next_folder = params.get('next_folder') or None

        if not token:
            raise YemotApiError(
                'חסרה הגדרת חובה בשלוחה: יש להגדיר api_add לפרמטר token'
            )

        extension = params.get('ApiExtension', '') or params.get('Extension', '')
        ext = extension if extension.startswith('/') else '/' + extension
        folder_ivr_path = normalize_ivr_path(ext)

        # --- שלב 1: אין עדיין הקלטה -> מבקשים מהמאזין להקליט ---
        # מזהים "אין הקלטה" לפי מספר הפנייה (val_1 ריק/לא קיים, כפי שמתועד
        # במודול ה-API: כל שלב נוסף מגיע כ-val_<n> עם התשובה של השלב הקודם).
        has_recording_answer = any(k.startswith('val_') for k in params)

        if not has_recording_answer:
            # מוצאים את המספר הפנוי הבא (000, 001, 002...) לפי קבצים קיימים בשלוחה
            next_number = find_next_free_number(token, folder_ivr_path)
            return response_ask_for_recording(next_number)

        # --- שלב 2: ההקלטה כבר קיימת בשלוחה -> מורידים, מתמללים, כותבים TTS ---
        # מוצאים שוב את המספר האחרון שנוצר (ההקלטה שזה עתה הושלמה) -
        # זהו המספר הפנוי-לשעבר, כלומר אחד פחות מהמספר הפנוי הנוכחי.
        next_free = find_next_free_number(token, folder_ivr_path)
        last_number = f'{int(next_free) - 1:03d}'

        recording_path = build_recording_path(extension, last_number)
        wav_bytes = download_recording(token, recording_path)
        text = transcribe_wav_bytes(wav_bytes)

        if not text:
            text = ''  # קובץ TTS ריק - עדיף מאשר לא לכתוב כלום, כדי לאפס תוצאה קודמת

        tts_path = f'{folder_ivr_path}/{last_number}'
        upload_tts_text(token, tts_path, text)

        return response_route_next(next_folder)

    def _send_text(self, status: int, body_text: str):
        body = body_text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _log_error(self, exc: Exception):
        # לוג בלבד לצד השרת (Vercel logs) - אף פעם לא לחשוף טוקן/תוכן הקלטה.
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
