from dotenv import load_dotenv
load_dotenv()                     

from fastapi import FastAPI
app = FastAPI(title="GridWise LLM Optimizer")

@app.get("/health")
def health():
    return {"status": "ok"}
