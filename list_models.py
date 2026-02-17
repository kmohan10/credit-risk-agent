import os
import google.generativeai as genai

API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("ANTIGRAVITY_API_KEY")
genai.configure(api_key=API_KEY)

print("Available models:")
for m in genai.list_models():
  if 'generateContent' in m.supported_generation_methods:
    print(m.name)
