"""
对接阿里云OSS储存服务 / Cloudflare R2 (S3 兼容) 储存服务
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
from botocore.exceptions import ClientError

from app.constants.storage import StorageType

logger = logging.getLogger(__name__)

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
            self.oss_bucket_style = None
            self.auth = None
            self.bucket = None
            self.client = None
            self.bucket_name = None
            self.oss_key_prefix = None
            self.oss_domain = None
            self.oss_via_cdn = None
            self.cdn_url_key = None

    def init(self, config):
        """配置初始化"""
        self.storage_type = config["STORAGE_TYPE"]
        self.oss_bucket_style = config.get("OSS_BUCKET_STYLE", "S3")
        if self.storage_type == StorageType.OSS:
            if self.oss_bucket_style == "R2":
                self.client = boto3.client(
                    "s3",
                    endpoint_url=config["OSS_ENDPOINT"],
                    aws_access_key_id=config["OSS_ACCESS_KEY_ID"],
                    aws_secret_access_key=config["OSS_ACCESS_KEY_SECRET"],
                )
                self.bucket_name = config["OSS_BUCKET_NAME"]
                self.oss_key_prefix = "storage/"
            else:
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

            self.oss_domain = config["STORAGE_DOMAIN"]
            self.STORAGE_PATH = STORAGE_PATH

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
            if self.oss_bucket_style == "R2":
                key = self.oss_key_prefix + path + filename
                extra_kwargs = _boto3_put_kwargs_from_headers(headers)
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
            if self.oss_bucket_style == "R2":
                key = self.oss_key_prefix + path + filename
                if local_path:
                    try:
                        self.client.download_file(self.bucket_name, key, local_path)
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "404":
                            raise NoSuchKey(
                                status=404, headers={}, body={}, details={}
                            )
                        raise
                else:
                    try:
                        response = self.client.get_object(
                            Bucket=self.bucket_name, Key=key
                        )
                        return BytesIO(response["Body"].read())
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "404":
                            raise NoSuchKey(
                                status=404, headers={}, body={}, details={}
                            )
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
            if self.oss_bucket_style == "R2":
                key = self.oss_key_prefix + path + filename
                try:
                    self.client.head_object(Bucket=self.bucket_name, Key=key)
                    return True
                except ClientError:
                    return False
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
            if self.oss_bucket_style == "R2":
                if isinstance(filename, list):
                    if len(filename) == 0:
                        return
                    return self.client.delete_objects(
                        Bucket=self.bucket_name,
                        Delete={
                            "Objects": [{"Key": self.oss_key_prefix + path + name} for name in filename]
                        },
                    )
                return self.client.delete_object(
                    Bucket=self.bucket_name, Key=self.oss_key_prefix + path + filename
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
            if self.oss_bucket_style == "R2":
                return self._sign_r2_url(*args, **kwargs)
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

    def _sign_r2_url(
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
        生成 R2 (S3 兼容) 文件访问 URL

        - CDN 模式（OSS_VIA_CDN=True）：基于 STORAGE_DOMAIN 直出，由 CDN / 反向代理负责回源
        - 非 CDN 模式：通过 boto3 生成 S3 兼容预签名 URL
        """
        if self.oss_via_cdn:
            if oss_domain is None:
                oss_domain = self.oss_domain
            return oss_domain + path + parse.quote(filename)
        expires = int(expires)
        params = {
            "Bucket": self.bucket_name,
            "Key": self.oss_key_prefix + path + filename,
        }
        if download:
            params["ResponseContentDisposition"] = "attachment"
        return self.client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expires
        )
