# utils/utils.py
"""
多功能工具库：支持 Web 开发（低代码）、机器学习、爬虫等场景
"""
# ✅ Web 开发相关：安全处理用户输入、生成唯一 ID、参数校验等
# ✅ 机器学习相关：数据预处理、模型路径管理、指标计算辅助
# ✅ 爬虫相关：User-Agent 随机化、重试机制、HTML 清洗、保存结果
# ✅ 通用工具：日志、文件、时间、JSON、环境变量等
# 场景	推荐函数
# 低代码 Web 平台	generate_unique_id, sanitize_input, require_env
# 机器学习实验	split_dataframe, compute_metrics, save_model_artifact
# 网络爬虫	safe_request, extract_text_from_html, save_crawl_results
# 通用开发	setup_logger, ensure_dir, read_json/write_json
import os
import json
import uuid
import random
import logging
import datetime
import requests
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Callable
from functools import wraps

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

# ======================
# 通用配置与常量
# ======================

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

# 默认重试策略（用于爬虫）
DEFAULT_RETRY_TIMES = 3
DEFAULT_TIMEOUT = 10


# ======================
# 日志 & 路径
# ======================

def get_project_root() -> Path:
    """获取项目根目录（假设 utils.py 在项目根目录或子目录中）"""
    return Path(__file__).parent.resolve()


def setup_logger(name: str, log_file: Optional[Union[str, Path]] = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def ensure_dir(path: Union[str, Path]) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ======================
# Web 开发（低代码平台）工具
# ======================

def generate_unique_id(prefix: str = "") -> str:
    """生成带前缀的唯一 ID，适用于低代码组件、表单、流程实例等"""
    return f"{prefix}{uuid.uuid4().hex}"


def validate_email(email: str) -> bool:
    """简易邮箱格式校验（生产环境建议用 email-validator 库）"""
    return "@" in email and "." in email.split("@")[-1]


def sanitize_input(user_input: str) -> str:
    """对用户输入进行基础清洗（防 XSS 等）"""
    # 移除 HTML 标签（保留文本）
    soup = BeautifulSoup(user_input, "html.parser")
    return soup.get_text()


def require_env(var_name: str) -> str:
    """从环境变量中读取必要配置，若缺失则报错"""
    value = os.getenv(var_name)
    if not value:
        raise EnvironmentError(f"Environment variable '{var_name}' is required but not set.")
    return value


# ======================
# 机器学习工具
# ======================

def save_model_artifact(data: Any, path: Union[str, Path]) -> None:
    """保存模型或预处理对象（支持 pickle / joblib 扩展）"""
    import joblib
    joblib.dump(data, path)


def load_model_artifact(path: Union[str, Path]) -> Any:
    """加载模型或预处理对象"""
    import joblib
    return joblib.load(path)


def split_dataframe(df: pd.DataFrame, train_ratio: float = 0.8, random_state: int = 42) -> tuple:
    """按比例分割 DataFrame（训练集/测试集）"""
    df = df.sample(frac=1, random_state=random_state).reset_index(drop=True)
    n_train = int(len(df) * train_ratio)
    return df.iloc[:n_train], df.iloc[n_train:]


def compute_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> Dict[str, float]:
    """计算常见分类指标（准确率、精确率、召回率、F1）"""
    from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
    try:
        acc = accuracy_score(y_true, y_pred)
        prec = precision_score(y_true, y_pred, average='weighted', zero_division=0)
        rec = recall_score(y_true, y_pred, average='weighted', zero_division=0)
        f1 = f1_score(y_true, y_pred, average='weighted', zero_division=0)
        return {"accuracy": acc, "precision": prec, "recall": rec, "f1": f1}
    except Exception as e:
        return {"error": str(e)}


# ======================
# 爬虫工具
# ======================

def get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def safe_request(
        url: str,
        method: str = "GET",
        retries: int = DEFAULT_RETRY_TIMES,
        timeout: int = DEFAULT_TIMEOUT,
        **kwargs
) -> Optional[requests.Response]:
    """带重试机制的安全 HTTP 请求"""
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", get_random_user_agent())

    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as e:
            if attempt == retries:
                logging.warning(f"Failed to fetch {url} after {retries + 1} attempts: {e}")
                return None
            logging.debug(f"Retry {attempt + 1}/{retries + 1} for {url}")
    return None


def extract_text_from_html(html: str) -> str:
    """从 HTML 中提取纯文本"""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup(["script", "style"]):
        script.decompose()
    return " ".join(soup.stripped_strings)


def save_crawl_results(data: List[Dict], output_path: Union[str, Path], format: str = "json") -> None:
    """保存爬虫结果（支持 JSON / CSV）"""
    output_path = Path(output_path)
    ensure_dir(output_path.parent)

    if format == "json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    elif format == "csv":
        pd.DataFrame(data).to_csv(output_path, index=False, encoding="utf-8-sig")
    else:
        raise ValueError("Unsupported format. Use 'json' or 'csv'.")


# ======================
# 通用装饰器
# ======================

def retry_on_failure(max_retries: int = 3, delay: float = 1.0, exceptions: tuple = (Exception,)):
    """通用重试装饰器（可用于爬虫、API 调用等）"""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if i == max_retries - 1:
                        raise e
                    time.sleep(delay)
            return None

        return wrapper

    return decorator


# ======================
# 时间 & 数据格式
# ======================

def now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.datetime.now().strftime(fmt)


def read_json(file_path: Union[str, Path]) -> Dict:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(data: Dict, file_path: Union[str, Path], indent: int = 2) -> None:
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


# ======================
# 示例入口（开发时可删除）
# ======================

if __name__ == "__main__":
    logger = setup_logger("utils_demo")
    logger.info("Utils loaded successfully.")

    # 示例：生成唯一ID
    print(generate_unique_id("form_"))

    # 示例：爬虫请求
    resp = safe_request("https://httpbin.org/get")
    if resp:
        print("Request OK")

    # 示例：保存模拟爬虫数据
    fake_data = [{"title": "Example", "url": "https://example.com"}]
    save_crawl_results(fake_data, "output/demo.json")