"""
提供邮件SMTP异步发送服务
"""

import email
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import render_template

from app import celery


@celery.task(name="tasks.email_task", time_limit=35)
def email_task(
    to_address,
    subject,
    html_content=None,
    text_content=None,
    reply_address=None,
    from_address=None,
    from_username=None,
    cc_address=None,
):
    """发送邮件"""
    if not celery.conf.app_config["ENABLE_USER_EMAIL"]:
        return "未开启用户邮件配置"
    email_smtp_host = celery.conf.app_config["EMAIL_SMTP_HOST"]
    email_smtp_port = celery.conf.app_config["EMAIL_SMTP_PORT"]
    email_use_ssl = celery.conf.app_config["EMAIL_USE_SSL"]
    email_address = celery.conf.app_config["EMAIL_ADDRESS"]
    email_username = celery.conf.app_config["EMAIL_USERNAME"]
    email_password = celery.conf.app_config["EMAIL_PASSWORD"]
    email_reply_address = celery.conf.app_config["EMAIL_REPLY_ADDRESS"]
    if from_address is None:
        from_address = email_address
    if from_username is None:
        from_username = email_username
    if reply_address is None:
        reply_address = email_reply_address
    # 处理收件人和抄送地址
    if isinstance(to_address, (list, tuple, set)):
        to_list = [str(addr).strip() for addr in to_address if addr and str(addr).strip()]
    elif to_address and str(to_address).strip():
        to_list = [str(to_address).strip()]
    else:
        to_list = []

    if isinstance(cc_address, (list, tuple, set)):
        cc_list = [str(addr).strip() for addr in cc_address if addr and str(addr).strip()]
    elif cc_address and str(cc_address).strip():
        cc_list = [str(cc_address).strip()]
    else:
        cc_list = []

    # 去重保持顺序
    envelope_recipients = []
    for addr in to_list + cc_list:
        if addr not in envelope_recipients:
            envelope_recipients.append(addr)

    if not envelope_recipients:
        return "发送失败，无收件人地址"

    # 构建alternative结构
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8").encode()
    msg["From"] = "%s <%s>" % (Header(from_username, "utf-8").encode(), from_address)
    if to_list:
        msg["To"] = ", ".join(to_list)
    elif cc_list:
        msg["To"] = ", ".join(cc_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Reply-to"] = reply_address  # 自定义的回复地址
    msg["Message-id"] = email.utils.make_msgid()
    msg["Date"] = email.utils.formatdate()
    # 构建alternative的text/html部分
    if html_content:
        text_html = MIMEText(html_content.encode(), _subtype="html", _charset="UTF-8")
        msg.attach(text_html)
    # 构建alternative的text/plain部分
    if text_content:
        text_plain = MIMEText(text_content.encode(), _subtype="plain", _charset="UTF-8")
        msg.attach(text_plain)
    # 发送邮件
    try:
        # 是否使用ssl
        if email_use_ssl:
            client = smtplib.SMTP_SSL(email_smtp_host, email_smtp_port)
        else:
            client = smtplib.SMTP(email_smtp_host, email_smtp_port)
        if not email_use_ssl:
            try:
                client.starttls()
                # client.ehlo_or_helo_if_needed()
            except smtplib.SMTPNotSupportedError:
                pass
        # 开启DEBUG模式
        client.set_debuglevel(0)
        client.login(email_username, email_password)
        # 发件人和认证地址必须一致
        # 备注：若想取到DATA命令返回值,可参考smtplib的sendmaili封装方法:
        #      使用SMTP.mail/SMTP.rcpt/SMTP.data方法
        client.sendmail(from_address, envelope_recipients, msg.as_string())
        client.quit()
        return "发送成功"
    except smtplib.SMTPConnectError as e:
        return "发送失败，连接失败:", str(e.smtp_code), str(e.smtp_error)
    except smtplib.SMTPAuthenticationError as e:
        return "发送失败，认证错误:", str(e.smtp_code), str(e.smtp_error)
    except smtplib.SMTPSenderRefused as e:
        return "发送失败，发件人被拒绝:", str(e.smtp_code), str(e.smtp_error)
    except smtplib.SMTPRecipientsRefused as e:
        return "发送失败，收件人被拒绝:", str(e.smtp_code), str(e.smtp_error)
    except smtplib.SMTPDataError as e:
        return "发送失败，数据接收拒绝:", str(e.smtp_code), str(e.smtp_error)
    except smtplib.SMTPException as e:
        return "发送失败, ", str(e.message)
    except Exception as e:
        return "发送异常, ", str(e)


def send_email(
    to_address,
    subject,
    html_content=None,
    text_content=None,
    reply_address=None,
    from_address=None,
    from_username=None,
    template=None,
    template_data=None,
    cc_address=None,
):
    # 如果提供了模板，则使用模板创建内容
    if template:
        html_content = render_template(template + ".html", **template_data)
        text_content = render_template(template + ".txt", **template_data)
    email_task.delay(
        to_address,
        subject,
        html_content,
        text_content,
        reply_address,
        from_address,
        from_username,
        cc_address,
    )
