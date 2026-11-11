"""全局配置：所有可调项集中在此，禁止业务代码散读环境变量。

设计要点：
1. 使用 pydantic-settings，字段名即环境变量名（前缀 GIPS_）。
2. 「固定长度 + 重叠」切分在本产品中被禁用，配置层直接拒绝，让服务无法启动，
   而不是只写一句文档约定（对应用例 AC-4）。
3. 所有路径默认相对仓库根目录，便于在仓库根目录下直接运行。
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 允许的切分策略：固定长度（fixed_length）不在其中，这是产品级禁令
ALLOWED_CHUNK_STRATEGIES = (
    "provision",      # 条款边界切分，A 类默认
    "section",        # 章节切分，B 类默认
    "paragraph",      # 段落切分（无重叠），兜底默认
    "table_cell",     # 表格单元切分
    "semantic",       # 语义切分，仅前三类不可用时
)

# 判定为「禁用策略」的别名集合，用于把配置错误指向同一条禁令
FORBIDDEN_CHUNK_STRATEGIES = (
    "fixed_length",
    "fixed_length_overlap",
    "sliding_window",
)


class Settings(BaseSettings):
    """服务配置。实例化即校验，校验失败即拒绝启动。"""

    model_config = SettingsConfigDict(
        env_prefix="GIPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 基础 ----------
    env: Literal["dev", "test", "prod"] = "dev"          # 运行环境
    app_name: str = "gips-review-rag"                    # 服务名，写入日志与 trace
    log_level: str = "INFO"                              # 日志级别

    # ---------- 存储 ----------
    db_path: Path = Path("var/gips.db")                   # SQLite 单文件台账
    object_store_dir: Path = Path("var/objects")          # 内容寻址对象存储根目录
    milvus_uri: str = "http://127.0.0.1:19530"            # Milvus standalone
    milvus_collection: str = "gips_chunks"                # chunk 集合名
    redis_url: str = "redis://127.0.0.1:6379/0"           # 语义缓存

    # ---------- 本地模型 ----------
    embed_model_path: Path = Path("models/bge-m3")               # 嵌入权重
    reranker_model_path: Path = Path("models/bge-reranker-v2-m3")  # 重排权重
    gguf_path: Path = Path("models/generate/qwen3-8b-q4_k_m.gguf")  # 生成权重
    # 生成后端：fake 用于无 GGUF 时跑通接口；llama_cpp 为进程内真实推理
    llm_backend: Literal["fake", "llama_cpp"] = "fake"
    llm_n_ctx: int = Field(default=4096, ge=512, le=8192)  # 8GB 卡上硬上限 4096
    llm_n_gpu_layers: int = -1                              # -1 表示全部层上卡
    llm_n_batch: int = 256                                  # 批大小，控制峰值显存
    llm_ctx_budget_ratio: float = Field(default=0.85, gt=0, lt=1)  # 上下文预算红线
    gpu_peak_limit_gb: float = Field(default=7.5, gt=0)     # AC-12A 的峰值阈值
    gpu_switch_timeout_s: int = 120                          # 单次模型切换超时

    # ---------- 解析 ----------
    text_layer_min_chars_page: int = 200   # 每页字符数 ≥ 200 视为有文本层（不送 OCR）
    text_layer_scan_max_chars_page: int = 50  # 每页字符数 < 50 视为扫描件
    header_margin_pt: int = 60             # 页眉安全带（pt）
    footer_margin_pt: int = 48             # 页脚安全带（pt）
    cleaner_version: str = "cleaner-v2"    # 清洗规则版本，变更即影响锚点
    chunker_version: str = "chunker-v2"    # 切分器代码版本，必须进 index_version

    # ---------- 切分（禁用项在此拦截） ----------
    chunker_strategy: str = "provision"    # 默认策略
    chunker_overlap: int = 0               # 必须为 0；非 0 即拒绝启动
    chunker_target_chars: int = 550        # 目标块长（字符）
    chunker_max_chars: int = 1100          # 单块硬上限
    chunker_min_chars: int = 60            # 尾块合并阈值

    # ---------- 检索 ----------
    retrieval_rrf_k: int = Field(default=60, ge=1)            # RRF 常数，PRD 5.5 固定 60
    retrieval_channel_timeout_ms: int = Field(default=800, ge=50)   # 单通道超时（契约 2.2）
    # 冷启动豁免：bge-m3 / reranker 首次加载远超 800ms，若套用常规超时，
    # 第一个请求必然 504 且加载线程会继续占卡，造成后续请求连环超时
    retrieval_warmup_timeout_s: float = Field(default=120.0, gt=0)
    retrieval_sql_timeout_s: float = Field(default=5.0, gt=0)       # 结构化通道超时
    retrieval_sql_max_rows: int = Field(default=200, ge=1)          # 结构化通道硬限行
    retrieval_rerank_timeout_s: float = Field(default=1.0, gt=0)    # 重排超时，超时降级 RRF 原序
    retrieval_dense_topk: int = Field(default=20, ge=1, le=100)     # 稠密通道召回数
    retrieval_sparse_topk: int = Field(default=20, ge=1, le=100)    # 稀疏通道召回数
    retrieval_rerank_top_provisions: int = Field(default=5, ge=1)   # 条款级重排后的名额

    # ---------- 索引 ----------
    embedding_dim: int = 1024              # bge-m3 稠密维度
    index_sample_check_size: int = 20      # active 前的抽样回查条数
    quality_min_parse_success: float = 0.98   # 解析成功率下限
    quality_max_empty_chunk_rate: float = 0.02  # 空块率上限
    quality_max_table_misalign_rate: float = 0.05  # 表格错位率上限

    # ---------- 出域与云 OCR ----------
    egress_enabled: bool = True                          # 出域网关总开关
    ocr_provider_order: str = "aliyun,tencent"           # 兜底=换供应商，不回退本地
    aliyun_ak: str = ""                                  # 阿里云 AccessKeyId
    aliyun_sk: str = ""                                  # 阿里云 AccessKeySecret
    tencent_ak: str = ""                                 # 腾讯云 SecretId
    tencent_sk: str = ""                                 # 腾讯云 SecretKey
    ocr_crop_margin_x: int = 40                          # 出域前左右裁边（像素）
    ocr_crop_margin_y: int = 40                          # 出域前上下裁边（像素）
    ocr_render_dpi: int = 200                            # 页面栅格化 DPI
    ocr_timeout_s: int = 30                              # 单页 OCR 超时

    @property
    def ocr_providers(self) -> list[str]:
        """把逗号分隔的供应商顺序解析为列表。"""
        return [p.strip() for p in self.ocr_provider_order.split(",") if p.strip()]

    @model_validator(mode="after")
    def _reject_forbidden_chunking(self) -> "Settings":
        """AC-4：固定长度带重叠切分必须让服务拒绝启动，而不是静默降级。"""
        if self.chunker_overlap != 0:
            raise ValueError(
                "配置校验失败：禁止使用「固定长度 + 重叠」切分（chunker_overlap="
                f"{self.chunker_overlap}）。重叠会使同一段内容落进两个 chunk，"
                "span_text 在两处都能匹配，回验器无法判断原始锚点。请将 GIPS_CHUNKER_OVERLAP 设为 0。"
            )
        if self.chunker_strategy in FORBIDDEN_CHUNK_STRATEGIES:
            raise ValueError(
                f"配置校验失败：禁止的切分策略 {self.chunker_strategy}。"
                f"允许的策略为 {', '.join(ALLOWED_CHUNK_STRATEGIES)}。"
            )
        if self.chunker_strategy not in ALLOWED_CHUNK_STRATEGIES:
            raise ValueError(
                f"配置校验失败：未知切分策略 {self.chunker_strategy}，"
                f"允许的策略为 {', '.join(ALLOWED_CHUNK_STRATEGIES)}。"
            )
        return self

    @model_validator(mode="after")
    def _check_single_gpu_budget(self) -> "Settings":
        """8GB 卡的上下文上限是硬约束，配置超过 4096 直接拒绝。"""
        if self.llm_n_ctx > 4096:
            raise ValueError(
                "配置校验失败：llm_n_ctx 不得超过 4096。8GB 显存下 KV cache 与权重"
                "合计会顶到上限，超限必然 OOM。"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例；测试可通过 get_settings.cache_clear() 重置。"""
    return Settings()


def reset_settings_cache() -> None:
    """测试辅助：清掉配置缓存，让新的环境变量生效。"""
    get_settings.cache_clear()
