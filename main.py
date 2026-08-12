"""启动入口。

热更新的 overlay 必须在导入 app.* 之前挂上 —— 一旦 app.api 被导入，
后面再改 sys.path 也换不掉已经进内存的模块。所以这里先 activate 再启动。
"""
if __name__ == "__main__":
    # 只导入 app.core.overlay 这一个模块。它零依赖，且不会牵连出 app 包里
    # 其他"应该由 overlay 覆盖"的代码
    from app.core import overlay

    running_version = overlay.activate()

    import uvicorn

    print(f"cinefold {running_version} 启动中", flush=True)

    uvicorn.run(
        app="app.api:app",
        host="0.0.0.0",
        port=56168,
        reload=False,
        log_level="error",
    )
