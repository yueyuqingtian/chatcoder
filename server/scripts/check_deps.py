try:
    import uvicorn, fastapi, sqlalchemy, aiosqlite, pydantic_settings
    print("deps OK")
except Exception as e:
    print("deps MISSING:", e)
