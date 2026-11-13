import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Config:
    SECRET = os.environ.get("APP_SECRET", "cc-etl-lab-dev-secret")
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    LAB_NAME = "抽取转换加载台"
    # 落库占位后的模拟耗时（秒），撑开争用窗口；测试可用 ETL_LOAD_DELAY 调小
    LOAD_DELAY = float(os.environ.get("ETL_LOAD_DELAY", "0.2"))

    @staticmethod
    def database() -> str:
        return os.environ.get("DATABASE_PATH", str(ROOT / "instance" / "etl_lab.db"))
