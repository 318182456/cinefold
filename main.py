"""启动入口。

热更新的 overlay 必须在导入 app.* 之前挂上 —— 一旦 app.api 被导入，
后面再改 sys.path 也换不掉已经进内存的模块。所以这里先 activate 再启动。
"""
if __name__ == "__main__":
    import time

    _boot = time.perf_counter()

    # 只导入 app.core.overlay 这一个模块。它零依赖，且不会牵连出 app 包里
    # 其他"应该由 overlay 覆盖"的代码
    from app.core import overlay

    running_version = overlay.activate()

    import uvicorn

    print(f"cinefold {running_version} 启动中", flush=True)

    # 导入 app.api 会连带拉起 FastAPI / SQLAlchemy 等重依赖，机器慢时能到十几秒。
    # 这段时间接口还没监听，页面上只看得到"后台启动中"，所以在这里显式报一下进度
    print("正在加载依赖…", flush=True)
    _import_start = time.perf_counter()
    from app.api import app as _app
    print(f"依赖加载完成，耗时 {time.perf_counter() - _import_start:.1f}s", flush=True)
    print(f"启动 Web 服务，进程已运行 {time.perf_counter() - _boot:.1f}s", flush=True)

    uvicorn.run(
        # 传对象而不是 "app.api:app"，上面已经导入过了，用字符串会再走一遍导入解析
        app=_app,
        host="0.0.0.0",
        port=56168,
        reload=False,
        log_level="error",
    )
