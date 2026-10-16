"""CloudDrive2 离线下载接入，与 qB/TR 本地做种链路分开。"""
from urllib.parse import urlsplit

from app.core.config import get_settings


def add_offline(magnet: str, save_path: str = "") -> dict:
    import grpc
    from app.modules.cloudnas import clouddrive_pb2 as pb
    from app.utils import get_magnet_hash

    settings = get_settings()
    if not get_magnet_hash(magnet) or not magnet.lower().startswith("magnet:?"):
        raise ValueError("请提供有效磁链")
    if not settings.cloudnas_url or not settings.cloudnas_username or not settings.cloudnas_password:
        raise ValueError("请先在设置中配置 CloudDrive 地址、用户名与密码")
    parsed = urlsplit(settings.cloudnas_url if "://" in settings.cloudnas_url else "http://" + settings.cloudnas_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("CloudDrive 地址格式无效")
    path = save_path or settings.cloudnas_savepath
    if not path.startswith("/") or ".." in path.split("/"):
        raise ValueError("请填写 CloudDrive 内的绝对保存路径")
    channel = grpc.secure_channel(parsed.netloc, grpc.ssl_channel_credentials()) if parsed.scheme == "https" else grpc.insecure_channel(parsed.netloc, options=[('grpc.enable_http_proxy', 0)])
    try:
        token_call = channel.unary_unary('/clouddrive.CloudDriveFileSrv/GetToken', request_serializer=pb.GetTokenRequest.SerializeToString, response_deserializer=pb.JWTToken.FromString)
        token = token_call(pb.GetTokenRequest(userName=settings.cloudnas_username, password=settings.cloudnas_password), timeout=20)
        if not token.success or not token.token:
            raise ValueError("CloudDrive 认证失败")
        add_call = channel.unary_unary('/clouddrive.CloudDriveFileSrv/AddOfflineFiles', request_serializer=pb.AddOfflineFileRequest.SerializeToString, response_deserializer=pb.FileOperationResult.FromString)
        result = add_call(pb.AddOfflineFileRequest(urls=magnet, toFolder=path), metadata=[('authorization', f'Bearer {token.token}')], timeout=30)
        if not result.success:
            raise ValueError("CloudDrive 未接受离线任务，请检查云盘权限与保存目录")
        return {"accepted": True, "save_path": path}
    finally:
        channel.close()
