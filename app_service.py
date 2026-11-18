"""Atomic input analysis, separate from Streamlit rendering."""
import hashlib
import io
import json
import time

import pandas as pd

from app_quality import build_quality_view, validate_final_mapping
from app_types import ActiveInput, AnalysisResult, EngineStatus, ReviewSnapshot, validate_history
from liushao import reviews as review_store
from liushao.data import read_csv_checked
from liushao.model import infer

MAX_BYTES = 50 * 1024 * 1024
MAX_ROWS = 50000
_ENGINE_CACHE = {}


ENGINE_DEGRADED = {
    'missing': '研判引擎文件缺失（engine.joblib 或 engine.json），本次只显示 RF/LR 基础检测结果。',
    'tampered': '研判引擎文件未通过完整性校验（哈希不符或文件损坏），已停用引擎，本次只显示 RF/LR 基础检测结果。',
    'version_mismatch': '研判引擎与当前底座模型或程序版本不匹配，已停用引擎，本次只显示 RF/LR 基础检测结果。',
    'runtime_error': '研判引擎运行出错，已停用引擎，本次只显示 RF/LR 基础检测结果。',
}


def _degraded(code):
    return EngineStatus(code, ENGINE_DEGRADED[code])


def load_trusted_engine_status(artifacts, bundle):
    """Load the exact verified bytes; returns (engine or None, EngineStatus).

    Identity includes parameter AND descriptor hash.  Application fallback does
    not bypass the standard launcher's whole-package check.
    """
    try:
        import liushao_engine
        path, descriptor = artifacts / 'engine.joblib', artifacts / 'engine.json'
        if not path.is_file() or not descriptor.is_file():
            return None, _degraded('missing')
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        descriptor_bytes = descriptor.read_bytes()
        try:
            meta = json.loads(descriptor_bytes)
            expected_digest, base_version = meta['engine_sha256'], meta['base_model_version']
        except Exception:
            return None, _degraded('tampered')
        if digest != expected_digest:
            return None, _degraded('tampered')
        if base_version != bundle['model_version'] or meta.get('format', 1) not in liushao_engine.SUPPORTED_FORMATS:
            return None, _degraded('version_mismatch')
        descriptor_digest = hashlib.sha256(descriptor_bytes).hexdigest()
        key = (digest, descriptor_digest, bundle['model_version'])
        if key not in _ENGINE_CACHE:
            import joblib
            try:
                engine = joblib.load(io.BytesIO(content))
            except Exception:
                return None, _degraded('tampered')
            if (not isinstance(engine, dict) or engine.get('format') not in liushao_engine.SUPPORTED_FORMATS
                    or engine.get('format') != meta.get('format', 1)
                    or engine.get('model_version') != bundle['model_version']
                    or liushao_engine.engine_version(engine) != meta['engine_version']):
                return None, _degraded('version_mismatch')
            engine = dict(engine)
            from app_engine_inference import inference_version
            engine['_artifact_identity'] = {
                'engine_sha256': digest, 'engine_descriptor_sha256': descriptor_digest,
                'engine_version': meta['engine_version'], 'base_model_version': bundle['model_version'],
                'base_model_sha256': meta['base_model_sha256'],
                'engine_inference_version': inference_version(engine)}
            _ENGINE_CACHE.clear()
            _ENGINE_CACHE[key] = engine
        return _ENGINE_CACHE[key], EngineStatus('ok', '')
    except Exception:
        return None, _degraded('runtime_error')


def load_trusted_engine(artifacts, bundle):
    return load_trusted_engine_status(artifacts, bundle)[0]


def with_engine_status(pred, features, bundle, artifacts):
    """Append trusted-triage columns; any failure keeps the frozen 1.3.6 predictions and says why."""
    engine, status = load_trusted_engine_status(artifacts, bundle)
    if engine is None:
        return pred, status
    try:
        from app_engine_inference import analyze_current_engine
        result = analyze_current_engine(pred, features, bundle, engine)
        from app_engine_evidence import IDENTITY_COLUMNS
        for name in IDENTITY_COLUMNS:
            result[name] = engine['_artifact_identity'][name]
        return result, status
    except Exception:
        return pred, _degraded('runtime_error')


def with_engine(pred, features, bundle, artifacts):
    return with_engine_status(pred, features, bundle, artifacts)[0]


def analyze_input(content, name, source, bundle, model_identity, artifacts, *, preflight=None):
    tick = time.perf_counter()
    if len(content) > MAX_BYTES:
        raise ValueError('文件超过 50 MiB，请分成较小的文件后再上传。')
    digest = hashlib.sha256(content).hexdigest()
    raw = preflight.take(content, model_identity) if preflight is not None else None
    if raw is None:
        raw = read_csv_checked(io.BytesIO(content), max_rows=MAX_ROWS)
    if raw.empty or len(raw) > MAX_ROWS:
        raise ValueError('文件需要包含 1 至 50,000 条记录。')
    pred, features, rejected = infer(raw, bundle, digest, name)
    pred, engine_status = with_engine_status(pred, features, bundle, artifacts)
    physical = pred.source_row.copy()
    original = pred.copy()
    if source == '内置演示数据':
        metadata_bytes = (artifacts / 'demo.json').read_bytes()
        if model_identity.split('|')[0] == '3c5b425f3a34454bd2b5a2738535742cc03e0f8f6e55fefc5abc5929c4dccad7':
            if hashlib.sha256(metadata_bytes).hexdigest() != 'f659acedda59cea6b4bdf8952146f7273c7e7bf4122454daa96dad703d353789':
                raise ValueError('演示描述不符合可信冻结身份，请恢复配套文件。')
        metadata = json.loads(metadata_bytes)
        origins_bytes = (artifacts / 'demo_origins.parquet').read_bytes()
        if (digest != metadata['csv_sha256'] or
                hashlib.sha256(origins_bytes).hexdigest() != metadata['origins_sha256']):
            raise ValueError('演示文件与来源信息不匹配，请恢复配套文件。')
        origins = pd.read_parquet(io.BytesIO(origins_bytes))
        if len(origins) != len(pred) or len(rejected):
            raise ValueError('演示记录与来源信息不匹配。')
        for col in ['record_id', 'source_file', 'source_sha256', 'source_row', 'timestamp']:
            pred[col] = origins[col]
        validate_final_mapping(original, pred)
    try:
        quality = build_quality_view(raw, original, features, rejected, final_predictions=pred)
        error = None
    except Exception as exc:
        quality, error = None, str(exc)
    from app_engine_evidence import engine_identity
    engine_key = engine_identity(pred.iloc[0]) if len(pred) else ('', '')
    active = ActiveInput(content, name, source, digest,
                         '|'.join((digest, source, name, model_identity, *engine_key)), model_identity)
    return active, AnalysisResult(pred, features, rejected, time.perf_counter()-tick, len(raw), engine_status), physical, quality, error



def read_review_snapshot(path, model_version, record_ids):
    """One live query; release unrelated history before returning to rendering."""
    rows = validate_history(review_store.history(path, model_version=model_version, record_ids=record_ids, include_evidence=False), model_version)
    audit = [item for item in rows if item['record_id'] in record_ids]
    from app_record_tools import latest_reviews
    return ReviewSnapshot(latest_reviews(audit, model_version), audit)
