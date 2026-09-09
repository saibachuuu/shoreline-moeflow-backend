"""
模型
"""

import logging
from mongoengine import connect
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def connect_db(config):
    logger.info("Connect mongodb")
    uri = config["DB_URI"]
    logger.debug(" - $DB_URI: {}".format(uri))
    kwargs = {}

    # MongoEngine 0.29 removed the ``mongomock://`` shortcut. Keep the test
    # configuration readable while passing its supported client class instead.
    if urlsplit(uri).scheme == "mongomock":
        import mongoengine

        if tuple(map(int, mongoengine.__version__.split(".")[:2])) >= (0, 29):
            import mongomock

            uri = uri.replace("mongomock://", "mongodb://", 1)
            kwargs["mongo_client_class"] = mongomock.MongoClient

    return connect(host=uri, **kwargs)


# TODO 为所有模型添加索引
