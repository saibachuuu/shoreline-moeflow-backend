"""存储池的后端适配与跨服务商搬运。

两条设计约束：

1. **不复用全局 ``OSS`` 单例**：那个单例绑死站点默认桶，且没有 list/head/copy。
2. **鸭子类型必须对齐 ``OSS``**：``sign_url`` / ``download`` / ``upload`` /
   ``delete`` / ``is_exist`` / ``list_objects`` / ``head_object`` / ``copy_object``，
   因为它会被塞进 ``storage_router`` 直接顶替全局 ``oss`` 使用。
"""

import logging
import os
import tempfile
from io import BytesIO

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError  # noqa: F401  (head/download 里仍在用)

from app.constants.storage import BucketStyle

logger = logging.getLogger(__name__)

_S3_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def _is_not_found(error) -> bool:
    code = str(
        (getattr(error, "response", None) or {}).get("Error", {}).get("Code", "")
    )
    return code in _S3_NOT_FOUND_CODES


def _strip_etag(etag):
    if not etag:
        return etag
    if isinstance(etag, bytes):
        etag = etag.decode("utf-8", "replace")
    return etag.strip('"')


class PoolBackend:
    """把一条 ``StoragePoolSource`` 包成与 ``OSS`` 同签名的读写后端。

    所有方法都工作在**相对 key** 空间（如 ``files/xxx.png``），
    子路径（``sub_path``）由本类内部加减，调用方看不到。
    """

    def __init__(self, source):
        self.source = source
        self.bucket_style = BucketStyle.S3
        self.oss_key_prefix = ""
        self.bucket_name = source.bucket
        self.oss_domain = (source.public_domain or "").rstrip("/") + "/"
        self.oss_via_cdn = bool(source.public_domain)
        self.client = boto3.client(
            "s3",
            endpoint_url=source.endpoint,
            aws_access_key_id=source.access_key_id,
            aws_secret_access_key=source.secret_access_key,
            region_name=source.region or "auto",
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": source.addressing_style or "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
            verify=source.verify_ssl,
        )
        self._prefix = source.normalized_sub_path()

    # ---- key 换算 ----------------------------------------------------
    @property
    def key_prefix(self) -> str:
        return self._prefix

    def _to_object_key(self, key: str) -> str:
        return self._prefix + key

    def _from_object_key(self, object_key: str) -> str:
        if self._prefix and object_key.startswith(self._prefix):
            return object_key[len(self._prefix) :]
        return object_key

    # ---- 与 OSS 对齐的接口 -------------------------------------------
    def sign_url(
        self,
        path,
        filename,
        expires=604800,
        oss_domain=None,
        process_name=None,
        download=False,
        **kwargs,
    ):
        if self.oss_via_cdn:
            return (oss_domain or self.oss_domain) + path + filename
        params = {
            "Bucket": self.bucket_name,
            "Key": self._to_object_key(path + filename),
        }
        if download:
            params["ResponseContentDisposition"] = "attachment"
        return self.client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=int(expires)
        )

    def download(self, path, filename, /, *, local_path=None):
        from oss2.exceptions import NoSuchKey

        key = self._to_object_key(path + filename)
        try:
            if local_path:
                self.client.download_file(self.bucket_name, key, local_path)
                return None
            response = self.client.get_object(Bucket=self.bucket_name, Key=key)
            return BytesIO(response["Body"].read())
        except ClientError as error:
            if _is_not_found(error):
                raise NoSuchKey(status=404, headers={}, body={}, details={})
            raise

    def upload(self, path, filename, file, headers=None, progress_callback=None):
        from app.services.oss import _boto3_put_kwargs_from_headers, _boto3_read_body

        key = self._to_object_key(path + filename)
        extra_kwargs = _boto3_put_kwargs_from_headers(headers)
        if hasattr(file, "read"):
            upload_kwargs = {}
            if extra_kwargs:
                upload_kwargs["ExtraArgs"] = extra_kwargs
            return self.client.upload_fileobj(
                file, self.bucket_name, key, **upload_kwargs
            )
        return self.client.put_object(
            Bucket=self.bucket_name,
            Key=key,
            Body=_boto3_read_body(file),
            **extra_kwargs,
        )

    def is_exist(self, path, filename, process_name=None):
        try:
            self.client.head_object(
                Bucket=self.bucket_name, Key=self._to_object_key(path + filename)
            )
            return True
        except ClientError as error:
            if _is_not_found(error):
                return False
            raise

    def delete(self, path, filename):
        if isinstance(filename, list):
            if not filename:
                return
            return self.client.delete_objects(
                Bucket=self.bucket_name,
                Delete={
                    "Objects": [
                        {"Key": self._to_object_key(path + name)} for name in filename
                    ]
                },
            )
        return self.client.delete_object(
            Bucket=self.bucket_name, Key=self._to_object_key(path + filename)
        )

    def list_objects(self, prefix: str = "", *, start_after=None, max_keys: int = 1000):
        kwargs = {
            "Bucket": self.bucket_name,
            "Prefix": self._to_object_key(prefix),
            "MaxKeys": max_keys,
        }
        if start_after:
            kwargs["StartAfter"] = self._to_object_key(start_after)
        resp = self.client.list_objects_v2(**kwargs)
        objects = [
            {
                "key": self._from_object_key(item["Key"]),
                "size": item.get("Size", 0),
                "etag": _strip_etag(item.get("ETag")),
                "last_modified": item.get("LastModified"),
            }
            for item in resp.get("Contents", [])
        ]
        truncated = bool(resp.get("IsTruncated"))
        next_cursor = objects[-1]["key"] if truncated and objects else None
        return {"objects": objects, "next": next_cursor}

    def head_object(self, key: str):
        try:
            resp = self.client.head_object(
                Bucket=self.bucket_name, Key=self._to_object_key(key)
            )
        except ClientError as error:
            if _is_not_found(error):
                return None
            raise
        return {
            "key": key,
            "size": resp.get("ContentLength", 0),
            "etag": _strip_etag(resp.get("ETag")),
            "last_modified": resp.get("LastModified"),
        }

    def copy_object(self, source_key: str, target_key: str):
        return self.client.copy_object(
            Bucket=self.bucket_name,
            CopySource={
                "Bucket": self.bucket_name,
                "Key": self._to_object_key(source_key),
            },
            Key=self._to_object_key(target_key),
        )

    # ---- 只在本模块用的能力 -------------------------------------------
    def head_bucket(self):
        """连通性测试第一步：能不能访问这个桶（只读，不写任何东西）。"""
        return self.client.head_bucket(Bucket=self.bucket_name)

    def __repr__(self):
        return f"<PoolBackend {self.source.name} {self.bucket_name}/{self._prefix}>"


def split_key(key: str):
    """把相对 key 拆成 ``(path, filename)``，与 ``OSS.download/upload`` 的参数对齐。

    ``files/abc.png`` → ``("files/", "abc.png")``
    ``abc.png``      → ``("", "abc.png")``
    """
    if "/" in key:
        head, _, tail = key.rpartition("/")
        return head + "/", tail
    return "", key


class S3Transfer:
    """把对象从一个后端搬到另一个后端。

    同一 endpoint、不同桶 → 服务端复制（不过本站带宽）；
    否则 → 下载到临时文件再上传（流式，不整块进内存）。

    不用跨桶 ``copy_object`` 去猜能不能用，是因为部分 S3 兼容实现（如 R2）
    不支持跨桶 CopyObject，猜错的代价是一大片对象搬运失败。
    """

    def __init__(self, src, dst):
        self.src = src
        self.dst = dst
        self._same_endpoint = self._endpoint_of(src) == self._endpoint_of(dst)
        # 同一个单例就没什么可搬的，用它兜住「源和目的是同一个桶」的配置错误。
        self._same_backend = src is dst

    @staticmethod
    def _endpoint_of(backend):
        client = getattr(backend, "client", None)
        meta = getattr(client, "meta", None)
        return getattr(meta, "endpoint_url", None)

    def transfer(self, key: str, *, size: int = 0):
        """搬一个对象，返回 ``(size, etag)``。异常向上抛，由调用方记为失败。"""
        if self._same_backend:
            raise ValueError("源与目的是同一个存储后端，无需迁移")
        if self._same_endpoint:
            try:
                self.dst.copy_object(key, key)
                meta = self.dst.head_object(key)
                if meta:
                    return meta["size"], meta.get("etag") or ""
            except Exception:
                # 服务端复制不可用（跨桶、实现不支持、本地存储没有源文件…）
                # 就退回流式搬运，不把整个任务判死。
                logger.warning("服务端复制失败，退回流式搬运：%s", key, exc_info=True)
        return self._stream_transfer(key, size)

    def _stream_transfer(self, key, size):
        path, filename = split_key(key)
        fd, temp_path = tempfile.mkstemp(prefix="moeflow-pool-")
        os.close(fd)
        try:
            self.src.download(path, filename, local_path=temp_path)
            with open(temp_path, "rb") as file:
                self.dst.upload(path, filename, file)
            meta = self.dst.head_object(key)
            return (meta["size"], meta.get("etag") or "") if meta else (size, "")
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                logger.warning("临时文件清理失败：%s", temp_path)
