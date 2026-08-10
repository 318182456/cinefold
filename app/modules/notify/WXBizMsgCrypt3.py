"""企业微信回调消息加解密。

按腾讯官方示例实现，用于验证回调 URL 与解密上行消息。
"""
from __future__ import annotations

import base64
import hashlib
import random
import socket
import string
import struct
import time
import xml.etree.cElementTree as ET

from Crypto.Cipher import AES


class WXBizMsgCryptError:
    OK = 0
    ValidateSignature_Error = -40001
    ParseXml_Error = -40002
    ComputeSignature_Error = -40003
    IllegalAesKey = -40004
    ValidateCorpid_Error = -40005
    EncryptAES_Error = -40006
    DecryptAES_Error = -40007
    IllegalBuffer = -40008


def throw_exception(message: str, exception_class=Exception):
    raise exception_class(message)


class SHA1:
    @staticmethod
    def getSHA1(token: str, timestamp: str, nonce: str, encrypt: str):
        try:
            parts = sorted([token, timestamp, nonce, encrypt])
            digest = hashlib.sha1("".join(parts).encode()).hexdigest()
            return WXBizMsgCryptError.OK, digest
        except Exception:
            return WXBizMsgCryptError.ComputeSignature_Error, None


class XMLParse:
    AES_TEXT_RESPONSE_TEMPLATE = """<xml>
<Encrypt><![CDATA[%(msg_encrypt)s]]></Encrypt>
<MsgSignature><![CDATA[%(msg_signaturet)s]]></MsgSignature>
<TimeStamp>%(timestamp)s</TimeStamp>
<Nonce><![CDATA[%(nonce)s]]></Nonce>
</xml>"""

    def extract(self, xmltext: str):
        try:
            root = ET.fromstring(xmltext)
            return WXBizMsgCryptError.OK, root.find("Encrypt").text, (
                root.find("ToUserName").text if root.find("ToUserName") is not None else None
            )
        except Exception:
            return WXBizMsgCryptError.ParseXml_Error, None, None

    def generate(self, encrypt: str, signature: str, timestamp: str, nonce: str) -> str:
        return self.AES_TEXT_RESPONSE_TEMPLATE % {
            "msg_encrypt": encrypt,
            "msg_signaturet": signature,
            "timestamp": timestamp,
            "nonce": nonce,
        }


class PKCS7Encoder:
    block_size = 32

    def encode(self, text: bytes) -> bytes:
        amount_to_pad = self.block_size - (len(text) % self.block_size)
        if amount_to_pad == 0:
            amount_to_pad = self.block_size
        return text + (chr(amount_to_pad) * amount_to_pad).encode()

    def decode(self, decrypted: bytes) -> bytes:
        pad = decrypted[-1]
        if pad < 1 or pad > 32:
            pad = 0
        return decrypted[:-pad] if pad else decrypted


class Prpcrypt:
    def __init__(self, key: bytes):
        self.key = key
        self.mode = AES.MODE_CBC

    def encrypt(self, text: str, receiveid: str):
        text_bytes = text.encode()
        payload = (
            self.get_random_str().encode()
            + struct.pack("I", socket.htonl(len(text_bytes)))
            + text_bytes
            + receiveid.encode()
        )
        try:
            padded = PKCS7Encoder().encode(payload)
            cryptor = AES.new(self.key, self.mode, self.key[:16])
            ciphertext = cryptor.encrypt(padded)
            return WXBizMsgCryptError.OK, base64.b64encode(ciphertext).decode()
        except Exception:
            return WXBizMsgCryptError.EncryptAES_Error, None

    def decrypt(self, text: str, receiveid: str):
        try:
            cryptor = AES.new(self.key, self.mode, self.key[:16])
            plain = cryptor.decrypt(base64.b64decode(text))
        except Exception:
            return WXBizMsgCryptError.DecryptAES_Error, None

        try:
            content = PKCS7Encoder().decode(plain)[16:]
            xml_len = socket.ntohl(struct.unpack("I", content[:4])[0])
            xml_content = content[4:xml_len + 4].decode()
            from_receiveid = content[xml_len + 4:].decode()
        except Exception:
            return WXBizMsgCryptError.IllegalBuffer, None

        if from_receiveid != receiveid:
            return WXBizMsgCryptError.ValidateCorpid_Error, None
        return WXBizMsgCryptError.OK, xml_content

    @staticmethod
    def get_random_str() -> str:
        return "".join(random.choice(string.ascii_letters) for _ in range(16))


class WXBizMsgCrypt:
    def __init__(self, sToken: str, sEncodingAESKey: str, sReceiveId: str):
        try:
            self.key = base64.b64decode(sEncodingAESKey + "=")
            assert len(self.key) == 32
        except Exception:
            throw_exception("[error]: EncodingAESKey 非法", FormatException)
        self.m_sToken = sToken
        self.m_sReceiveId = sReceiveId

    def VerifyURL(self, sMsgSignature, sTimeStamp, sNonce, sEchoStr):
        ret, signature = SHA1.getSHA1(self.m_sToken, sTimeStamp, sNonce, sEchoStr)
        if ret != 0:
            return ret, None
        if signature != sMsgSignature:
            return WXBizMsgCryptError.ValidateSignature_Error, None
        return Prpcrypt(self.key).decrypt(sEchoStr, self.m_sReceiveId)

    def EncryptMsg(self, sReplyMsg, sNonce, timestamp=None):
        ret, encrypt = Prpcrypt(self.key).encrypt(sReplyMsg, self.m_sReceiveId)
        if ret != 0:
            return ret, None
        timestamp = timestamp or str(int(time.time()))
        ret, signature = SHA1.getSHA1(self.m_sToken, timestamp, sNonce, encrypt)
        if ret != 0:
            return ret, None
        return ret, XMLParse().generate(encrypt, signature, timestamp, sNonce)

    def DecryptMsg(self, sPostData, sMsgSignature, sTimeStamp, sNonce):
        ret, encrypt, _ = XMLParse().extract(sPostData)
        if ret != 0:
            return ret, None
        ret, signature = SHA1.getSHA1(self.m_sToken, sTimeStamp, sNonce, encrypt)
        if ret != 0:
            return ret, None
        if signature != sMsgSignature:
            return WXBizMsgCryptError.ValidateSignature_Error, None
        return Prpcrypt(self.key).decrypt(encrypt, self.m_sReceiveId)


class FormatException(Exception):
    pass
