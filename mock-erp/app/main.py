from fastapi import FastAPI

app = FastAPI(title="Mock ERP")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
