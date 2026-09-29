import os
from dotenv import load_dotenv
from google import genai

load_dotenv()
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
for m in client.models.list():
    n = m.name.lower()
    if "live" in n or "native-audio" in n:
        print(m.name)