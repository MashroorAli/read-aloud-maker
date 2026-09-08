from flask import Flask, render_template, request, session, redirect
from google.oauth2.credentials import Credentials
from googleapiclient.http import MediaFileUpload
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow
from google.oauth2 import service_account
from google.cloud import texttospeech
from datetime import datetime
import pdfplumber
import requests
import json
import os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

if os.environ.get("FLASK_ENV") != "production":
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

CLIENT_SECRETS_FILE = "credentials/oauth_client.json"
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://127.0.0.1:5000/oauth2callback")

os.makedirs("uploads", exist_ok=True)
os.makedirs("outputs", exist_ok=True)

OAUTH_CLIENT_CONFIG = os.environ.get("OAUTH_CLIENT_CONFIG")

def get_oauth_flow(state=None):
    if OAUTH_CLIENT_CONFIG:
        client_config = json.loads(OAUTH_CLIENT_CONFIG)
        return Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            state=state,
            redirect_uri=REDIRECT_URI
        )
    else:
        return Flow.from_client_secrets_file(
            CLIENT_SECRETS_FILE,
            scopes=SCOPES,
            state=state,
            redirect_uri=REDIRECT_URI
        )
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid"
    ]

GOOGLE_TTS_CREDENTIALS = os.environ.get("GOOGLE_TTS_CREDENTIALS")

if GOOGLE_TTS_CREDENTIALS:
    tts_credentials_dict = json.loads(GOOGLE_TTS_CREDENTIALS)
    tts_credentials = service_account.Credentials.from_service_account_info(tts_credentials_dict)
else:
    tts_credentials = None

@app.route("/")
def home():
    is_connected = "credentials" in session
    user_name = session.get("user_name")
    past_folders = []

    if is_connected:
        try:
            creds_dict = session["credentials"]
            credentials = Credentials(**creds_dict)
            drive_service = build("drive", "v3", credentials=credentials)

            query = "mimeType='application/vnd.google-apps.folder' and appProperties has { key='created_by' and value='read-aloud-maker' }"

            results = drive_service.files().list(
                q=query,
                fields="files(id, name, createdTime)"
            ).execute()

            past_folders = results.get("files", [])

            for folder in past_folders:
                parsed_date = datetime.fromisoformat(folder["createdTime"])
                folder["createdTime"] = parsed_date.strftime("%b %d, %Y")

        except Exception:
            session.clear()
            is_connected = False
            user_name = None

    return render_template("index.html", is_connected=is_connected, user_name=user_name, past_folders=past_folders)

def parse_mc_questions(lines):
    question_bank = []
    curr_question = []

    for line in lines:
        dot_pos = line.find(".")
        par_pos = line.find(")")

        if dot_pos == -1 and par_pos == -1:
            continue        # not a question line
        elif dot_pos == -1:
            p = par_pos     # question line with ')'
        elif par_pos == -1:
            p = dot_pos     # question line with '.'
        else:
            p = min(dot_pos, par_pos)  # if both exist, take the first one

        if line[:p].isdigit():  # check if the characters up until the delimiter is a digit
            if curr_question:
                question_bank.append("<speak>" + " ".join(curr_question) + "</speak>")
                curr_question.clear() # adds the completed question to the bank, then resets it for the next question

            curr_question.append(line)

        elif line[:p].isalpha() and len(line[:p]) == 1: # check if the starting question is a MC answer
            new_line = line[0].upper() + "." + "<break time = '200ms'/>" + line[2:] + "." + "<break time = '500ms'/>"
            curr_question.append(new_line)

        else:
            print("this is line: ", line)

    if curr_question: # after all pages are done, adds the last question 
        question_bank.append("<speak>" + " ".join(curr_question) + "</speak>")

    return question_bank

@app.route("/upload", methods=["post"])
def upload_file():
    if "credentials" not in session:
        return "Please connect your Google Drive account first."

    # ***** initialization of variables *****

    # pdf plumber
    uploaded_file = request.files["pdf_file"]
    folder_name = request.form["folder_name"]

    # make sure the uploaded file is a pdf before saving
    if not uploaded_file.filename.endswith(".pdf"):
        return "Please upload a PDF file."

    save_path = os.path.join("uploads", uploaded_file.filename)
    uploaded_file.save(save_path)

    # google tts
    if tts_credentials:
        client = texttospeech.TextToSpeechClient(credentials=tts_credentials)
    else:
        client = texttospeech.TextToSpeechClient()

    voice = texttospeech.VoiceSelectionParams(
        language_code = "en-US",
        name = "en-US-Wavenet-D"
    )

    audio_config = texttospeech.AudioConfig(
        audio_encoding = texttospeech.AudioEncoding.MP3
    )

    # ***** parses pdf here *****
    with pdfplumber.open(save_path) as pdf:
        all_lines = []

        for page in pdf.pages:
            text = page.extract_text()
            all_lines.extend(text.split("\n"))

    question_bank = parse_mc_questions(all_lines)

    # ***** this part of the code creates the TTS files *****
    audio_files = []

    if not question_bank:
        return "Looks like your file might not follow the required format for uploading. Please double check."

    for i, question_text in enumerate(question_bank):
        synthesis_input = texttospeech.SynthesisInput(ssml=question_text)

        response = client.synthesize_speech(
            input = synthesis_input, voice = voice, audio_config = audio_config
        )

        filename = f"outputs/question_{i+1}.mp3"

        with open(filename, "wb") as out:
            out.write(response.audio_content)

        audio_files.append(filename)

    # ***** creates the google drive session & links *****
    creds_dict = session["credentials"]
    credentials = Credentials(**creds_dict)
    drive_service = build("drive", "v3", credentials=credentials)
    drive_links = []

    folder_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "appProperties": {
            "created_by" : "read-aloud-maker"
        }
    }

    folder = drive_service.files().create(
        body = folder_metadata,
        fields = "id"
    ).execute()

    folder_id = folder.get("id")

    drive_service.permissions().create(
        fileId = folder_id,
        body = {"type": "anyone", "role": "reader"}
    ).execute()

    folder_link = f"https://drive.google.com/drive/folders/{folder_id}"

    for file_path in audio_files:
        file_metadata = {
            "name": os.path.basename(file_path),
            "parents" : [folder_id]
            }
        media = MediaFileUpload(file_path, mimetype = "audio/mpeg")

        uploaded_file = drive_service.files().create(
            body = file_metadata,
            media_body = media,
            fields = "id"
        ).execute()

        file_id = uploaded_file.get("id")

        drive_service.permissions().create(
            fileId = file_id,
            body = {"type": "anyone", "role": "reader"}
        ).execute()

        link = f"http://drive.google.com/file/d/{file_id}/view"
        drive_links.append(link)

    return render_template("results.html", folder_link=folder_link)

@app.route("/authorize")
def authorize():
    flow = get_oauth_flow()

    authorization_url, state = flow.authorization_url(
        access_type = "offline",
        prompt = "consent"
    )
    session["state"] = state
    session["code_verifier"] = flow.code_verifier
    return redirect(authorization_url)

@app.route("/oauth2callback")
def oauth2callback():
    flow = get_oauth_flow(state=session["state"])

    flow.code_verifier = session["code_verifier"]
    flow.fetch_token(authorization_response = request.url)
    credentials = flow.credentials

    user_info_response = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {credentials.token}"}
    )
    user_info = user_info_response.json()

    session["user_name"] = user_info.get("name")

    session["credentials"] = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes
    }

    return """
    <!DOCTYPE html>
    <html>
    <body>
        <script>
            window.close();
        </script>
        <p>You can close this tab.</p>
    </body>
    </html>
    """

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/connection_status")
def connection_status():
    return {"connected": "credentials" in session}

@app.route("/delete_folder/<folder_id>")
def delete_folder(folder_id):
    if "credentials" not in session:
        return "Please connect your Google Drive account first."

    creds_dict = session["credentials"]
    credentials = Credentials(**creds_dict)
    drive_service = build("drive", "v3", credentials=credentials)

    drive_service.files().delete(fileId = folder_id).execute()

    return redirect("/")

if __name__ == "__main__":
    app.run(debug=True)