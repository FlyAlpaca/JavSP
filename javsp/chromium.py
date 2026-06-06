"""解析Chromium系浏览器Cookies的相关函数

支持 Windows / Linux / macOS 三个平台的 Chrome Cookie 解密：
- Windows: DPAPI 解密 Local State 中的 master key → AES-256-GCM 解密 cookie
- Linux: v10 使用固定密码 "peanuts" PBKDF2 派生 key → AES-128-CBC 解密;
         v11 使用 keyring (secretstorage) 获取密码 → PBKDF2 派生 key → AES-128-CBC 解密
- macOS: Keychain 获取 "Chrome Safe Storage" 密码 → PBKDF2 派生 key → AES-128-CBC 解密
"""

import base64
import json
import logging
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from glob import glob
from hashlib import pbkdf2_hmac
from shutil import copyfile

__all__ = ["get_browsers_cookies"]

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

logger = logging.getLogger(__name__)

# Linux/macOS Chrome 使用的 PBKDF2 参数
_PBKDF2_SALT = b"saltysalt"
_PBKDF2_ITERATIONS_LINUX = 1
_PBKDF2_ITERATIONS_MAC = 1003
_PBKDF2_KEY_LENGTH_LINUX = 16  # AES-128
_PBKDF2_KEY_LENGTH_MAC = 16  # AES-128
_CBC_IV = b" " * 16  # 0x20 * 16


class WindowsDecrypter:
    """Windows 平台使用 AES-256-GCM 解密 Chrome Cookie (v10/v11 前缀)"""

    def __init__(self, key: bytes):
        self.key = key

    def decrypt(self, encrypted_value: bytes) -> str:
        if encrypted_value[:3] not in (b"v10", b"v11"):
            # 旧版 DPAPI 直接加密，不应走到这里
            raise ValueError("不支持的加密格式")
        nonce = encrypted_value[3:15]
        ciphertext_tag = encrypted_value[15:]
        ciphertext = ciphertext_tag[:-16]
        tag = ciphertext_tag[-16:]
        cipher = AES.new(self.key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
        return plaintext


class PosixDecrypter:
    """Linux/macOS 平台使用 AES-128-CBC 解密 Chrome Cookie (v10/v11 前缀)"""

    def __init__(self, key: bytes):
        self.key = key

    def decrypt(self, encrypted_value: bytes) -> str:
        if encrypted_value[:3] not in (b"v10", b"v11"):
            raise ValueError("不支持的加密格式")
        ciphertext = encrypted_value[3:]
        cipher = AES.new(self.key, AES.MODE_CBC, iv=_CBC_IV)
        decrypted = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return decrypted.decode("utf-8")


def _get_browser_user_data_dirs() -> dict[str, str]:
    """返回当前平台上各浏览器的用户数据目录映射"""
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA")
        if not base:
            return {}
        return {
            "Chrome": os.path.join(base, "Google", "Chrome", "User Data"),
            "Chrome Beta": os.path.join(base, "Google", "Chrome Beta", "User Data"),
            "Chrome Canary": os.path.join(base, "Google", "Chrome SxS", "User Data"),
            "Chromium": os.path.join(base, "Google", "Chromium", "User Data"),
            "Edge": os.path.join(base, "Microsoft", "Edge", "User Data"),
            "Vivaldi": os.path.join(base, "Vivaldi", "User Data"),
        }
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
        return {
            "Chrome": os.path.join(base, "Google", "Chrome"),
            "Chrome Beta": os.path.join(base, "Google", "Chrome Beta"),
            "Chrome Canary": os.path.join(base, "Google", "Chrome Canary"),
            "Chromium": os.path.join(base, "Chromium"),
            "Edge": os.path.join(base, "Microsoft Edge"),
            "Vivaldi": os.path.join(base, "Vivaldi"),
            "Brave": os.path.join(base, "BraveSoftware", "Brave-Browser"),
        }
    else:  # Linux
        base = os.path.expanduser("~/.config")
        return {
            "Chrome": os.path.join(base, "google-chrome"),
            "Chrome Beta": os.path.join(base, "google-chrome-beta"),
            "Chrome Dev": os.path.join(base, "google-chrome-unstable"),
            "Chromium": os.path.join(base, "chromium"),
            "Edge": os.path.join(base, "microsoft-edge"),
            "Vivaldi": os.path.join(base, "vivaldi"),
            "Brave": os.path.join(base, "BraveSoftware", "Brave-Browser"),
        }


def _decrypt_key_win(local_state_path: str) -> bytes:
    """Windows: 从 Local State 提取并用 DPAPI 解密 master key

    Chrome 127+ 引入了 App-Bound Encryption (ABE)，加密密钥绑定到 Chrome 二进制文件，
    外部进程无法通过 DPAPI 解密。此函数仅适用于 Chrome < 127 或未启用 ABE 的场景。
    """
    import win32crypt

    with open(local_state_path, encoding="utf-8") as f:
        local_state = json.loads(f.read())

    # 检查是否存在 App-Bound Encryption 密钥（Chrome 127+）
    os_crypt = local_state.get("os_crypt", {})
    if "app_bound_encrypted_key" in os_crypt:
        logger.warning(
            "检测到 Chrome App-Bound Encryption (ABE)，Chrome 127+ 将 Cookie 加密密钥"
            "绑定到浏览器二进制文件，外部程序无法解密。建议使用 Firefox 浏览器的 Cookie。"
        )

    encrypted_key = os_crypt.get("encrypted_key", "")
    if not encrypted_key:
        raise ValueError("Local State 中未找到 encrypted_key")
    encrypted_key = base64.b64decode(encrypted_key)
    encrypted_key = encrypted_key[5:]  # 移除 DPAPI 前缀
    return win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]


def _get_keyring_password_linux() -> bytes | None:
    """Linux: 从 GNOME Keyring 或 KDE KWallet 获取 Chrome Safe Storage 密码

    优先尝试 secretstorage (GNOME Keyring / Secret Service API)，
    回退到 kwallet-query (KDE KWallet)。
    """
    # 尝试 GNOME Keyring (secretstorage)
    password = _get_secretstorage_password()
    if password is not None:
        return password

    # 回退到 KDE KWallet
    password = _get_kwallet_password()
    if password is not None:
        return password

    return None


def _get_secretstorage_password() -> bytes | None:
    """通过 secretstorage 从 GNOME Keyring 获取 Chrome Safe Storage 密码"""
    try:
        import secretstorage
    except ImportError:
        logger.debug("secretstorage 未安装，跳过 GNOME Keyring")
        return None

    try:
        connection = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(connection)
        if collection is None or collection.is_locked():
            logger.debug("Keyring 集合不可用或已锁定")
            return None
        for item in collection.get_all_items():
            if item.get_label() in ("Chrome Safe Storage", "Chromium Safe Storage"):
                return item.get_secret()
    except Exception as e:
        logger.debug(f"从 GNOME Keyring 获取密码失败: {e}", exc_info=True)
    return None


def _get_kwallet_password() -> bytes | None:
    """通过 kwallet-query 从 KDE KWallet 获取 Chrome Safe Storage 密码"""
    try:
        import subprocess

        # KWallet 使用 "Chrome Keys" 文件夹存储浏览器密钥
        # 先尝试 kwallet-query (KDE 5+)
        result = subprocess.run(
            [
                "kwallet-query",
                "kdewallet",
                "readPassword",
                "Chrome Keys",
                "Chrome Safe Storage",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().encode("utf-8")

        # Chromium 使用不同的条目名
        result = subprocess.run(
            [
                "kwallet-query",
                "kdewallet",
                "readPassword",
                "Chrome Keys",
                "Chromium Safe Storage",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().encode("utf-8")
    except FileNotFoundError:
        logger.debug("kwallet-query 未安装，跳过 KDE KWallet")
    except Exception as e:
        logger.debug(f"从 KDE KWallet 获取密码失败: {e}", exc_info=True)
    return None


def _get_keychain_password_mac() -> bytes | None:
    """macOS: 从 Keychain 获取 Chrome Safe Storage 密码"""
    try:
        import subprocess

        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().encode("utf-8")
        # Chromium 使用不同的服务名
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", "Chromium Safe Storage"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().encode("utf-8")
    except Exception as e:
        logger.debug(f"从 Keychain 获取密码失败: {e}", exc_info=True)
    return None


def _create_decrypter_win(local_state_path: str) -> WindowsDecrypter | None:
    """创建 Windows 平台的解密器"""
    try:
        key = _decrypt_key_win(local_state_path)
        return WindowsDecrypter(key)
    except Exception as e:
        logger.debug(f"Windows 密钥解密失败: {e}", exc_info=True)
        return None


def _create_decrypter_linux() -> PosixDecrypter | None:
    """创建 Linux 平台的解密器

    优先使用 v11 (keyring 密码)，回退到 v10 (固定密码 "peanuts")。
    两者都使用相同的 AES-CBC 解密流程，只是 PBKDF2 输入密码不同。
    """
    # 尝试从 keyring 获取密码 (v11)
    keyring_password = _get_keyring_password_linux()
    if keyring_password is not None:
        key = pbkdf2_hmac(
            "sha1",
            keyring_password,
            _PBKDF2_SALT,
            _PBKDF2_ITERATIONS_LINUX,
            _PBKDF2_KEY_LENGTH_LINUX,
        )
        return PosixDecrypter(key)

    # 回退到 v10 固定密码
    logger.debug("未找到 keyring 密码，使用 v10 固定密码")
    key = pbkdf2_hmac(
        "sha1",
        b"peanuts",
        _PBKDF2_SALT,
        _PBKDF2_ITERATIONS_LINUX,
        _PBKDF2_KEY_LENGTH_LINUX,
    )
    return PosixDecrypter(key)


def _create_decrypter_mac() -> PosixDecrypter | None:
    """创建 macOS 平台的解密器"""
    password = _get_keychain_password_mac()
    if password is None:
        logger.debug("未找到 Keychain 密码，使用默认密码")
        password = b"peanuts"
    key = pbkdf2_hmac("sha1", password, _PBKDF2_SALT, _PBKDF2_ITERATIONS_MAC, _PBKDF2_KEY_LENGTH_MAC)
    return PosixDecrypter(key)


def get_browsers_cookies():
    """获取系统上的所有Chromium系浏览器的JavDB的Cookies"""
    # 不予支持: Opera, 360安全&极速, 搜狗使用非标的用户目录或数据格式; QQ浏览器屏蔽站点
    user_data_dirs = _get_browser_user_data_dirs()
    if not user_data_dirs:
        return []

    all_browser_cookies = []
    exceptions = []

    for brw, user_dir in user_data_dirs.items():
        if not os.path.isdir(user_dir):
            continue

        cookies_files = glob(os.path.join(user_dir, "*", "Cookies")) + glob(os.path.join(user_dir, "*", "Network", "Cookies"))

        for cookies_file in cookies_files:
            try:
                if sys.platform == "win32":
                    local_state = os.path.join(user_dir, "Local State")
                    if not os.path.exists(local_state):
                        continue
                    decrypter = _create_decrypter_win(local_state)
                elif sys.platform == "darwin":
                    decrypter = _create_decrypter_mac()
                else:
                    decrypter = _create_decrypter_linux()

                if decrypter is None:
                    continue

                # 提取 profile 名称
                rel = os.path.relpath(cookies_file, user_dir)
                parts = rel.split(os.sep)
                profile_name = parts[0] if parts else "Unknown"
                profile = f"{brw}: {profile_name}"

                records = get_cookies(cookies_file, decrypter)
                if records:
                    for site, cookies in records.items():
                        entry = {
                            "profile": profile,
                            "site": site,
                            "cookies": cookies,
                        }
                        all_browser_cookies.append(entry)
            except Exception as e:
                exceptions.append(e)
                logger.debug(f"无法解析Cookies文件({e}): {cookies_file}", exc_info=True)

    if len(all_browser_cookies) == 0 and len(exceptions) > 0:
        raise exceptions[0]
    return all_browser_cookies


def convert_chrome_utc(chrome_utc):
    """将Chrome存储的UTC时间转换为UNIX的UTC时间格式"""
    # Chrome's cookies timestamp's epoch starts 1601-01-01T00:00:00Z
    second = int(chrome_utc / 1e6)
    if second > 0:  # 考虑chrome_utc为0的情况
        second = second - 11644473600
    unix_utc = datetime.fromtimestamp(second)
    return unix_utc


def get_cookies(cookies_file, decrypter, host_pattern="javdb%.com"):
    """从cookies_file文件中查找指定站点的所有Cookies"""
    # 复制Cookies文件到临时目录，避免直接操作原始的Cookies文件
    # 使用 tempfile 确保文件名唯一且自动清理，避免竞态条件
    with tempfile.TemporaryDirectory(prefix="javsp_cookies_") as tmpdir:
        temp_cookie = os.path.join(tmpdir, "Cookies")
        # 同时复制 -wal 和 -shm 侧文件（如果存在），确保 SQLite 数据完整
        for suffix in ("", "-wal", "-shm"):
            src = cookies_file + suffix
            if os.path.exists(src):
                copyfile(src, temp_cookie + suffix)
        # 连接数据库进行查询
        conn = sqlite3.connect(temp_cookie)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT host_key, name, encrypted_value, expires_utc FROM cookies WHERE host_key LIKE ?",
            (host_pattern,),
        )
        # 将查询结果按照host_key进行组织
        now = datetime.now()
        records = {}
        for host_key, name, encrypted_value, expires_utc in cursor.fetchall():
            d = records.setdefault(host_key, {})
            # 只提取尚在有效期内的Cookies
            expires = convert_chrome_utc(expires_utc)
            if expires > now:
                try:
                    d[name] = decrypter.decrypt(encrypted_value)
                except Exception as e:
                    logger.debug(f"解密Cookie失败({e}): {name}@{host_key}", exc_info=True)
        # Cookies的核心字段是'_jdb_session'，因此如果records中缺失此字段（说明已过期），则对应的Cookies不再有效
        valid_records = {k: v for k, v in records.items() if "_jdb_session" in v}
        conn.close()
    return valid_records


if __name__ == "__main__":
    all_cookies = get_browsers_cookies()
    for d in all_cookies:
        print("{:<20}{}".format(d["profile"], d["site"]))
