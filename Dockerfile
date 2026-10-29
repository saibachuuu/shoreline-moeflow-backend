FROM python:3.12

LABEL project="moeflow-backend"

COPY ./requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# 单容器多进程编排：moeflow-celery-misc 用 supervisord 同时管理
# notification / security_email / beat 三个独立进程（见 deploy/supervisord.conf）。
RUN pip install --no-cache-dir "supervisor>=4.2,<5"

ARG MOEFLOW_BUILD_ID=unknown
ENV MOEFLOW_BUILD_ID=${MOEFLOW_BUILD_ID}

COPY . /app
WORKDIR /app

# 单容器多进程编排配置（moeflow-celery-misc 使用）
COPY ./deploy/supervisord.conf /etc/supervisor/conf.d/moeflow.conf

EXPOSE 5000

RUN BIN_PREFIX=/usr/local/bin make babel-update-mo
