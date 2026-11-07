"""追加缺失的本机 MySQL 配置并生成密钥；不覆盖已有配置，不输出密钥或密码。"""
from pathlib import Path
from cryptography.fernet import Fernet
from dotenv import dotenv_values


def configure(path: Path):
    existing = dotenv_values(path) if path.exists() else {}
    defaults = {"MYSQL_HOST": "localhost", "MYSQL_PORT": "3306", "MYSQL_DATABASE": "oa_agent",
                "MYSQL_USER": "", "MYSQL_PASSWORD": "", "MYSQL_SSL_CA": "", "OA_RELOGIN_TIMEOUT": "180"}
    lines = [f"{key}={value}" for key, value in defaults.items() if key not in existing]
    if not existing.get("SESSION_ENCRYPTION_KEY"):
        lines.append("SESSION_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
    if lines:
        with path.open("a") as handle:
            handle.write("\n# MySQL 会话持久化配置\n" + "\n".join(lines) + "\n")
        path.chmod(0o600)
    print("MySQL 默认配置已准备；请在本机 .env 填写 MYSQL_USER / MYSQL_PASSWORD，然后重启 Streamlit。")


if __name__ == "__main__":
    configure(Path(__file__).resolve().parents[1] / ".env")
