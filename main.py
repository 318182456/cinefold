"""启动入口。"""
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app="app.api:app",
        host="0.0.0.0",
        port=56168,
        reload=False,
        log_level="error",
    )
