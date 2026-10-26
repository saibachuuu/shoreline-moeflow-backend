"""
对接对象储存服务。

两种实现，由 ``OSS_BUCKET_STYLE`` 归一后决定（见 ``app.constants.storage``）：

* ``BucketStyle.S3``  标准 S3 兼容协议，走 boto3 —— AWS S3 / Cloudflare R2 /
  MinIO / Garage / 自建网关。对象 key 统一带 ``OSS_KEY_PREFIX`` 总前缀。
* ``BucketStyle.ALI`` 阿里云 OSS 原生 SDK（oss2），额外拥有服务端图片处理
  与 CDN URL 鉴权，因此缩略图不需要本程序生成。无总前缀（历史行为）。
"""

from io import BufferedReader, BytesIO, FileIO
import os
import re
import shutil
import time
import hashlib
import logging
from typing import Union
from urllib import parse

import oss2
from oss2 import to_string
from oss2.exceptions import NoSuchKey

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.constants.storage import (
    BucketStyle,
    StorageType,
    parse_bucket_style,
    report_bucket_style_issues,
)

logger = logging.getLogger(__name__)

# S3 返回「对象不存在」时的错误码集合。
_S3_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def _is_not_found(error: ClientError) -> bool:
    """判断 boto3 异常是否表示对象不存在（不同实现回的 code 不一样）。"""
    code = str(
        (getattr(error, "response", None) or {}).get("Error", {}).get("Code", "")
    )
    return code in _S3_NOT_FOUND_CODES


def _strip_etag(etag):
    """去掉 ETag 两侧的引号，让各实现返回的形状一致；None 透传。"""
    if not etag:
        return etag
    if isinstance(etag, bytes):
        etag = etag.decode("utf-8", "replace")
    return etag.strip('"')


# HTTP 头部到 boto3 put_object 参数的映射
_BOTO3_HEADER_MAP = {
    "Cache-Control": "CacheControl",
    "Content-Disposition": "ContentDisposition",
    "Content-Encoding": "ContentEncoding",
    "Content-Language": "ContentLanguage",
    "Content-Type": "ContentType",
    "Expires": "Expires",
}


def _boto3_put_kwargs_from_headers(headers):
    """将 oss2 风格的 headers 转为 boto3 put_object 的关键字参数"""
    if not headers:
        return {}
    kwargs = {}
    for k, v in headers.items():
        param = _BOTO3_HEADER_MAP.get(k)
        if param:
            if isinstance(v, bytes):
                v = v.decode("utf-8")
            kwargs[param] = v
    return kwargs


def _boto3_read_body(file):
    """将 file 参数转为 boto3 put_object 的 Body 值"""
    if isinstance(file, str):
        return file.encode("utf-8")
    if isinstance(file, bytes):
        return file
    if hasattr(file, "read"):
        data = file.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        return data
    return file


def md5sum(src):
    m = hashlib.md5()
    m.update(src)
    return m.hexdigest()


def aliyun_cdn_url_auth_c(uri, key, exp):
    """阿里云 CDN 鉴权方式 C"""
    p = re.compile("^(http://|https://)?([^/?]+)(/[^?]*)?(\\?.*)?$")
    if not p:
        return None
    m = p.match(uri)
    scheme, host, path, args = m.groups()
    if not scheme:
        scheme = "http://"
    if not path:
        path = "/"
    if not args:
        args = ""
    hexexp = "%x" % exp
    sstring = key + path + hexexp
    hashvalue = md5sum(sstring.encode("utf-8"))
    return "%s%s/%s/%s%s%s" % (scheme, host, hashvalue, hexexp, path, args)


class OSS:
    def __init__(self, config=None):
        if config:
            self.init(config)
        else:
            self.storage_type = None
            self.bucket_style = None
            self.capabilities = None
            self.auth = None
            self.bucket = None
            self.client = None
            self.bucket_name = None
            self.oss_key_prefix = ""
            self.oss_domain = None
            self.oss_via_cdn = None
            self.cdn_url_key = None

    def init(self, config):
        """配置初始化。每个进程调用一次即可（切换配置后必须重新调用）。"""
        self.storage_type = config["STORAGE_TYPE"]
        # 归一：R2/MINIO/GARAGE/AWS → S3，ALIYUN → ALI。顺带对危险配置打日志。
        _style = parse_bucket_style(
            config.get("OSS_BUCKET_STYLE"), config.get("OSS_ENDPOINT")
        )
        report_bucket_style_issues(_style)
        self.bucket_style = _style.style
        self.capabilities = _style.capabilities
        # 阿里云分支无总前缀（历史行为，不能动）。
        self.oss_key_prefix = config.get("OSS_KEY_PREFIX", "storage/") or ""
        self.bucket_name = config.get("OSS_BUCKET_NAME", "")

        if self.storage_type == StorageType.OSS:
            if self.bucket_style == BucketStyle.S3:
                self.client = boto3.client(
                    "s3",
                    endpoint_url=config["OSS_ENDPOINT"],
                    aws_access_key_id=config["OSS_ACCESS_KEY_ID"],
                    aws_secret_access_key=config["OSS_ACCESS_KEY_SECRET"],
                    region_name=config.get("OSS_S3_REGION") or "auto",
                    config=BotoConfig(
                        signature_version="s3v4",
                        s3={
                            "addressing_style": config.get("OSS_S3_ADDRESSING_STYLE")
                            or "path"
                        },
                        retries={"max_attempts": 3, "mode": "standard"},
                    ),
                    verify=config.get("OSS_S3_VERIFY_SSL", True),
                )
            else:
                # 阿里云分支没有总前缀。
                self.oss_key_prefix = ""
                self.auth = oss2.Auth(
                    config["OSS_ACCESS_KEY_ID"],
                    config["OSS_ACCESS_KEY_SECRET"],
                )
                self.bucket = oss2.Bucket(
                    self.auth,
                    config["OSS_ENDPOINT"],
                    config["OSS_BUCKET_NAME"],
                )

            self.oss_domain = config["STORAGE_DOMAIN"]
            self.oss_via_cdn = config["OSS_VIA_CDN"]
            self.cdn_url_key = config["CDN_URL_KEY_A"]
        else:
            from app import STORAGE_PATH

            self.oss_key_prefix = ""
            self.oss_domain = config["STORAGE_DOMAIN"]
            self.STORAGE_PATH = STORAGE_PATH

    # ------------------------------------------------------------------
    # key 换算：内部工作空间是「相对 key」（不含总前缀），即 path + filename。
    # 总前缀只在真正发请求时加、返回时剥掉，调用方不必关心。
    # ------------------------------------------------------------------
    def _to_object_key(self, key: str) -> str:
        return self.oss_key_prefix + key

    def _from_object_key(self, object_key: str) -> str:
        if self.oss_key_prefix and object_key.startswith(self.oss_key_prefix):
            return object_key[len(self.oss_key_prefix) :]
        return object_key

    def upload(
        self,
        path: str,
        filename: str,
        file: Union[str, BufferedReader, FileIO],
        headers=None,
        progress_callback=None,
    ):
        """上传文件"""
        if self.storage_type == StorageType.OSS:
            if self.bucket_style == BucketStyle.S3:
                key = self._to_object_key(path + filename)
                extra_kwargs = _boto3_put_kwargs_from_headers(headers)
                if hasattr(file, "read"):
                    # upload_fileobj performs multipart streaming for large
                    # files.  Do not turn an upload into a full in-memory Body.
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
            return self.bucket.put_object(
                path + filename,
                file,
                headers=headers,
                progress_callback=progress_callback,
            )
        else:
            folder_path = os.path.join(self.STORAGE_PATH, path)
            os.makedirs(folder_path, exist_ok=True)
            if isinstance(file, BufferedReader):
                with open(os.path.join(folder_path, filename), "wb") as saved_file:
                    saved_file.write(file.read())
            elif isinstance(file, str):
                with open(os.path.join(folder_path, filename), "w") as saved_file:
                    saved_file.write(file)
            else:
                file.save(
                    os.path.join(folder_path, filename)
                )  # XXX: what's the type of file here?
        logging.debug("saved file : %s / %s", folder_path, filename)

    def download(self, path, filename: str, /, *, local_path=None):
        """下载文件"""
        if self.storage_type == StorageType.OSS:
            if self.bucket_style == BucketStyle.S3:
                key = self._to_object_key(path + filename)
                if local_path:
                    try:
                        self.client.download_file(self.bucket_name, key, local_path)
                    except ClientError as e:
                        if _is_not_found(e):
                            raise NoSuchKey(status=404, headers={}, body={}, details={})
                        raise
                else:
                    try:
                        response = self.client.get_object(
                            Bucket=self.bucket_name, Key=key
                        )
                        return BytesIO(response["Body"].read())
                    except ClientError as e:
                        if _is_not_found(e):
                            raise NoSuchKey(status=404, headers={}, body={}, details={})
                        raise
            else:
                if local_path:
                    self.bucket.get_object_to_file(path + filename, local_path)
                else:
                    return self.bucket.get_object(path + filename)
        else:
            folder_path = os.path.join(self.STORAGE_PATH, path)
            file_path = os.path.join(folder_path, filename)
            if local_path:
                if self.is_exist(folder_path, filename):
                    shutil.copy2(file_path, local_path)
                else:
                    raise NoSuchKey(status=404, headers={}, body={}, details={})
            else:
                with open(file_path, "rb") as file:
                    return BytesIO(file.read())

    def is_exist(self, path, filename, process_name=None):
        """检查文件是否存在"""
        if self.storage_type == StorageType.OSS:
            if self.bucket_style == BucketStyle.S3:
                key = self._to_object_key(path + filename)
                try:
                    self.client.head_object(Bucket=self.bucket_name, Key=key)
                    return True
                except ClientError as error:
                    if _is_not_found(error):
                        return False
                    raise
            return self.bucket.object_exists(path + filename)
        else:
            if os.path.isabs(path):
                return os.path.isfile(
                    os.path.join(
                        path,
                        (process_name + "-" if process_name is not None else "")
                        + filename,
                    )
                )
            else:
                return os.path.isfile(
                    os.path.join(
                        self.STORAGE_PATH,
                        path,
                        (process_name + "-" if process_name is not None else "")
                        + filename,
                    )
                )

    def delete(self, path, filename: Union[str, list[str]]):
        """（批量）删除文件"""
        if self.storage_type == StorageType.OSS:
            if self.bucket_style == BucketStyle.S3:
                if isinstance(filename, list):
                    if len(filename) == 0:
                        return
                    return self.client.delete_objects(
                        Bucket=self.bucket_name,
                        Delete={
                            "Objects": [
                                {"Key": self._to_object_key(path + name)}
                                for name in filename
                            ]
                        },
                    )
                return self.client.delete_object(
                    Bucket=self.bucket_name,
                    Key=self._to_object_key(path + filename),
                )
            if isinstance(filename, list):
                if len(filename) == 0:
                    return
                result = self.bucket.batch_delete_objects(
                    [path + name for name in filename]
                )
            else:
                result = self.bucket.delete_object(path + filename)
            return result
        else:
            folder_path = os.path.join(self.STORAGE_PATH, path)
            if isinstance(filename, list):
                for name in filename:
                    if self.is_exist(folder_path, name):
                        os.remove(os.path.join(folder_path, name))
            else:
                if self.is_exist(folder_path, filename):
                    os.remove(os.path.join(folder_path, filename))

    def rmdir(self, path):
        """（批量）删除文件夹，仅本地储存"""
        if self.storage_type == StorageType.LOCAL_STORAGE:
            if isinstance(path, list):
                for p in path:
                    folder_path = os.path.join(self.STORAGE_PATH, p)
                    if os.path.isdir(folder_path) and len(os.listdir(folder_path)) == 0:
                        os.rmdir(folder_path)
            else:
                folder_path = os.path.join(self.STORAGE_PATH, path)
                if os.path.isdir(folder_path) and len(os.listdir(folder_path)) == 0:
                    os.rmdir(folder_path)

    def sign_url(self, *args, **kwargs):
        if self.storage_type == StorageType.OSS:
            if self.bucket_style == BucketStyle.S3:
                return self._sign_s3_url(*args, **kwargs)
            if self.oss_via_cdn:
                return self._sign_cdn_url(*args, **kwargs)
            return self._sign_oss_url(*args, **kwargs)
        else:
            return self._sign_local_url(*args, **kwargs)

    def _sign_local_url(
        self,
        path,
        filename,
        expires=604800,
        oss_domain=None,
        process_name=None,
        **kwargs,
    ):
        return (
            self.oss_domain
            + path
            + (process_name + "-" if process_name is not None else "")
            + filename
        )

    def _sign_cdn_url(
        self,
        path,
        filename,
        expires=604800,
        oss_domain=None,
        process_name=None,
        **kwargs,
    ):
        """
        通过 CDN 的 URL 鉴权生成可以访问的 URL，此时 oss_domain 需要是绑定于 CDN 的域名
        """
        now = int(time.time())
        delta = expires - now % expires
        expires = delta + 86400
        if oss_domain is None:
            oss_domain = self.oss_domain
        uri = oss_domain + path + parse.quote(filename)
        url = aliyun_cdn_url_auth_c(uri=uri, key=self.cdn_url_key, exp=now + expires)
        if process_name:
            url += f"?x-oss-process=style/{process_name}"
        return url

    def _sign_oss_url(
        self,
        path,
        filename,
        expires=604800,
        headers=None,
        params=None,
        method="GET",
        oss_domain=None,
        download=False,
        process_name=None,
    ):
        """
        通过 OSS 的 URL 签名生成可以访问的 URL，默认使用配置中用户自定义的 OSS 域名
        """
        delta = expires - int(time.time()) % expires
        expires = delta + 86400
        if oss_domain is None:
            oss_domain = self.oss_domain
        if params is None:
            params = {}
        if download:
            params["response-content-disposition"] = "attachment"
        if process_name:
            params["x-oss-process"] = f"style/{process_name}"
        key = to_string(path + filename)
        req = oss2.http.Request(
            method, oss_domain + parse.quote(key), headers=headers, params=params
        )
        return self.bucket.auth._sign_url(req, self.bucket.bucket_name, key, expires)

    def _sign_s3_url(
        self,
        path,
        filename,
        expires=604800,
        oss_domain=None,
        process_name=None,
        download=False,
        **kwargs,
    ):
        """
        生成标准 S3 兼容存储的访问 URL（R2 / MinIO / Garage / AWS S3 …）

        - CDN 模式（OSS_VIA_CDN=True）：基于 STORAGE_DOMAIN 直出，由 CDN / 反向代理负责回源
        - 非 CDN 模式：通过 boto3 生成预签名 URL
        """
        if self.oss_via_cdn:
            if oss_domain is None:
                oss_domain = self.oss_domain
            return oss_domain + path + parse.quote(filename)
        expires = int(expires)
        params = {
            "Bucket": self.bucket_name,
            "Key": self._to_object_key(path + filename),
        }
        if download:
            params["ResponseContentDisposition"] = "attachment"
        return self.client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires
        )

    # ------------------------------------------------------------------
    # 低层对象操作（存储池迁移使用）。
    #
    # 这三个方法全部工作在「相对 key」空间 —— 与 upload/download 的
    # path + filename 是同一坐标系，不含 OSS_KEY_PREFIX 总前缀。
    # 总前缀的加减只发生在这里内部，调用方永远看不到它。
    # ------------------------------------------------------------------
    def list_objects(self, prefix: str = "", *, start_after=None, max_keys: int = 1000):
        """列出对象（单页，不递归到上层目录语义，纯 key 前缀匹配）。

        :param prefix: 相对 key 前缀，例如 ``"files/"``
        :param start_after: 游标 —— 上一页返回的 ``next``
        :param max_keys: 单页上限
        :return: ``{"objects": [{"key","size","etag","last_modified"}], "next": 游标或 None}``
                 ``next`` 为空表示已到末尾。
        """
        if self.storage_type == StorageType.OSS:
            if self.bucket_style == BucketStyle.S3:
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
            else:
                # 阿里云分支无总前缀，prefix 原样透传。
                resp = self.bucket.list_objects_v2(
                    prefix=prefix,
                    start_after=start_after or "",
                    max_keys=max_keys,
                )
                objects = [
                    {
                        "key": item.key,
                        "size": item.size,
                        "etag": _strip_etag(item.etag),
                        "last_modified": item.last_modified,
                    }
                    for item in resp.object_list
                ]
                truncated = bool(resp.is_truncated)
        else:
            base = os.path.join(self.STORAGE_PATH, prefix)
            objects = []
            if os.path.isdir(base):
                for name in sorted(os.listdir(base)):
                    file_path = os.path.join(base, name)
                    if not os.path.isfile(file_path):
                        continue
                    objects.append(
                        {
                            "key": prefix + name,
                            "size": os.path.getsize(file_path),
                            # 本地存储不算 md5：调用方只能按 size 校验。
                            "etag": None,
                            "last_modified": None,
                        }
                    )
            truncated = False

        # 用「本页最后一个 key」作游标。不用厂商 token，好处是两种实现
        # 与断点续传记录都能存同一个字段，重放语义也更直观。
        next_cursor = objects[-1]["key"] if truncated and objects else None
        return {"objects": objects, "next": next_cursor}

    def head_object(self, key: str):
        """取单个对象的元信息；对象不存在返回 ``None``（不抛异常）。

        :param key: 相对 key，例如 ``"files/abc.png"``
        :return: ``{"key","size","etag","last_modified"}`` 或 ``None``
        """
        object_key = self._to_object_key(key)
        if self.storage_type == StorageType.OSS:
            if self.bucket_style == BucketStyle.S3:
                try:
                    resp = self.client.head_object(
                        Bucket=self.bucket_name, Key=object_key
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
            if not self.bucket.object_exists(object_key):
                return None
            meta = self.bucket.head_object(object_key)
            return {
                "key": key,
                "size": meta.content_length,
                "etag": _strip_etag(meta.etag),
                "last_modified": meta.last_modified,
            }
        file_path = os.path.join(self.STORAGE_PATH, key)
        if not os.path.isfile(file_path):
            return None
        return {
            "key": key,
            "size": os.path.getsize(file_path),
            "etag": None,
            "last_modified": None,
        }

    def copy_object(self, source_key: str, target_key: str):
        """同桶内服务端复制（不落地、不占带宽）。

        仅适用于同一个桶内搬 key。跨桶或跨服务商请用 ``S3Transfer``（阶段 3）。
        """
        if self.storage_type == StorageType.OSS:
            if self.bucket_style == BucketStyle.S3:
                return self.client.copy_object(
                    Bucket=self.bucket_name,
                    CopySource={
                        "Bucket": self.bucket_name,
                        "Key": self._to_object_key(source_key),
                    },
                    Key=self._to_object_key(target_key),
                )
            return self.bucket.copy_object(
                self.bucket_name,
                self._to_object_key(source_key),
                self._to_object_key(target_key),
            )
        source_path = os.path.join(self.STORAGE_PATH, source_key)
        target_path = os.path.join(self.STORAGE_PATH, target_key)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        return shutil.copy2(source_path, target_path)
