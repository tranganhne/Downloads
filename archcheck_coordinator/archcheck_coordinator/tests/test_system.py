from collections import Counter
from pathlib import Path

import pytest

from archcheck.agents import (ArchitectureAnalysisAgent, ConsistencyAnalysisAgent,
                              SourceCodeAnalysisAgent)
from archcheck.coordinator import AgentSet, CoordinatorConfig, CoordinatorService, default_agents
from archcheck.graph_utils import graph_from_dict
from archcheck.models import (ArchitectureTask, CodeAnalysisResult, CodeAnalysisTask, ConsistencyTask)
from archcheck.storage import Storage

SAMPLE = Path(__file__).resolve().parent.parent / 'sample'
PROJECT, PLAN = str(SAMPLE / 'shop'), str(SAMPLE / 'plan.puml')


class Counting:
    """Bọc Agent thật, đếm số lần được gọi; có thể giả lập lỗi."""
    def __init__(self, real, fail_times=0, fixed_result=None):
        self.real, self.calls, self.fail_times, self.fixed = real, 0, fail_times, fixed_result

    def run(self, task):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError('boom')
        return self.fixed if self.fixed is not None else self.real.run(task)


def make_service(tmp_path, arch=None, code=None, cons=None, rec=None, **cfg):
    storage = Storage(f'sqlite:///{tmp_path}/t.db')
    base = default_agents(storage)
    agents = AgentSet(architecture=arch or Counting(base.architecture),
                      source_code=code or Counting(base.source_code),
                      consistency=cons or Counting(base.consistency),
                      recommendation=rec or Counting(base.recommendation))
    agents.recommendation.record_feedback = base.recommendation.record_feedback
    return CoordinatorService(agents=agents, config=CoordinatorConfig(**cfg), storage=storage), agents


def test_source_agent_extracts_expected_graph():
    res = SourceCodeAnalysisAgent().run(CodeAnalysisTask(run_id='t', project_path=PROJECT))
    assert res.status == 'ok'
    G = graph_from_dict(res.graph)
    assert Counter(d['kind'] for _, d in G.nodes(data=True)) == {'module': 3, 'class': 3, 'function': 5}
    rel = lambda r: {(u, v) for u, v, d in G.edges(data=True) if d['rel'] == r}
    assert rel('imports') == {('api.order_controller', 'service.order_service'),
                              ('service.order_service', 'repo.order_repo'),
                              ('service.order_service', 'api.order_controller')}
    assert len(rel('calls')) == 4 and len(rel('contains')) == 8


def test_consistency_finds_violation_and_cycle():
    pa = ArchitectureAnalysisAgent().run(ArchitectureTask(run_id='t', plan_path=PLAN))
    ca = SourceCodeAnalysisAgent().run(CodeAnalysisTask(run_id='t', project_path=PROJECT))
    res = ConsistencyAnalysisAgent().run(ConsistencyTask(run_id='t', pa=pa, ca=ca))
    assert {v.type for v in res.violations} == {'forbidden_dependency', 'dependency_cycle'}
    fd = next(v for v in res.violations if v.type == 'forbidden_dependency')
    assert (fd.from_component, fd.to_component) == ('Service', 'Controller')
    assert fd.evidence[0].line == 2


def test_happy_path_order(tmp_path):
    svc, agents = make_service(tmp_path)
    state = svc.analyze(PROJECT, PLAN)
    assert state['status'] == 'done'
    trace = state['trace']
    idx = lambda name: next(i for i, t in enumerate(trace) if t.startswith(f'coordinator -> {name}'))
    assert idx('arch_agent') < idx('code_agent') < idx('consistency_agent') < idx('recommend_agent')
    assert all(a.calls == 1 for a in (agents.architecture, agents.source_code,
                                      agents.consistency, agents.recommendation))
    assert state['report']['violations']


def test_transient_failure_is_retried(tmp_path):
    arch = Counting(ArchitectureAnalysisAgent(), fail_times=2)
    svc, _ = make_service(tmp_path, arch=arch, max_retry=2)
    state = svc.analyze(PROJECT, PLAN)
    assert state['status'] == 'done' and arch.calls == 3


def test_retry_exhausted_stops_pipeline(tmp_path):
    arch = Counting(ArchitectureAnalysisAgent(), fail_times=99)
    svc, agents = make_service(tmp_path, arch=arch, max_retry=2)
    state = svc.analyze(PROJECT, PLAN)
    assert state['status'] == 'failed' and state['report']['failed_step'] == 'arch_agent'
    assert arch.calls == 3 and agents.source_code.calls == 0


def test_deterministic_failure_is_not_retried(tmp_path):
    bad = CodeAnalysisResult(status='failed', error='x', retryable=False)
    code = Counting(SourceCodeAnalysisAgent(), fixed_result=bad)
    svc, agents = make_service(tmp_path, code=code)
    state = svc.analyze(PROJECT, PLAN)
    assert state['status'] == 'failed' and code.calls == 1 and agents.consistency.calls == 0


def test_feedback_suppresses_next_run(tmp_path):
    svc, _ = make_service(tmp_path)
    first = svc.analyze(PROJECT, PLAN)['report']
    vid = next(v['id'] for v in first['violations'] if v['type'] == 'forbidden_dependency')
    svc.submit_feedback(vid, 'intended', 'chủ ý')
    second = svc.analyze(PROJECT, PLAN)['report']
    v2 = next(v for v in second['violations'] if v['id'] == vid)
    assert v2['recommendation']['suppressed'] is True


def test_missing_plan_fails_fast(tmp_path):
    svc, agents = make_service(tmp_path)
    state = svc.analyze(PROJECT, str(tmp_path / 'nope.puml'))
    assert state['status'] == 'failed' and agents.architecture.calls == 1
