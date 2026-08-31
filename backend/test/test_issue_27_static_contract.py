from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_operational_docs_state_t13_hard_disable_and_t14_enablement_boundary():
    agents = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
    runbook = (ROOT / 'docs/operations/purge-batch-pipeline-runbook.md').read_text(
        encoding='utf-8'
    )

    for text in (agents, runbook):
        assert 'T14' in text
        assert '硬性关闭' in text
        assert 'pending_deletion' in text
        assert 'FormalPurgeRepository' in text
