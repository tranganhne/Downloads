"""Giao diện demo Streamlit:  streamlit run streamlit_app.py"""
import streamlit as st

from archcheck.coordinator import CoordinatorService
from archcheck.models import AnalysisState  # noqa: F401  (tài liệu hóa kiểu State)

st.set_page_config(page_title='Kiểm tra nhất quán kiến trúc - mã nguồn', layout='wide')
st.title('Adaptive Multi-Agent: kiểm tra nhất quán kiến trúc - mã nguồn')


@st.cache_resource
def get_service() -> CoordinatorService:
    return CoordinatorService()


def build_dot(report: dict) -> str:
    pa = report.get('pa') or {}
    lines = ['digraph G { rankdir=LR; node [shape=box];']
    for c in pa.get('components', []):
        lines.append(f'"{c["name"]}";')
    for a, b in pa.get('allowed_dependencies', []):
        lines.append(f'"{a}" -> "{b}" [color=gray];')
    for v in report['violations']:
        if v['type'] == 'forbidden_dependency':
            lines.append(f'"{v["from_component"]}" -> "{v["to_component"]}" [color=red, label="vi phạm"];')
    lines.append('}')
    return '\n'.join(lines)


with st.sidebar:
    project_path = st.text_input('Đường dẫn dự án Python', 'sample/shop')
    plan_path = st.text_input('Tài liệu kiến trúc (.puml / .json)', 'sample/plan.puml')
    run = st.button('Chạy phân tích', type='primary')

service = get_service()
if run:
    with st.spinner('Coordinator đang điều phối các Agent...'):
        st.session_state['report'] = service.analyze(project_path, plan_path)['report']

report = st.session_state.get('report')
if report:
    (st.success if report['status'] == 'done' and not report['violations'] else st.warning)(report['verdict'])
    for w in report['warnings']:
        st.info(w)
    for e in report['errors']:
        st.error(f'{e.get("agent")}: {e.get("error")}')

    tab1, tab2, tab3 = st.tabs(['Vi phạm & đề xuất', 'Sơ đồ component', 'Trace điều phối'])
    with tab1:
        for v in report['violations']:
            rec = v.get('recommendation') or {}
            title = f'[{v["severity"]}] {v["type"]}: {v["message"]}'
            with st.expander(title + (' (đã ẩn)' if rec.get('suppressed') else '')):
                if rec.get('suppressed'):
                    st.caption(rec.get('suppress_reason'))
                st.write(rec.get('explanation', ''))
                for a in rec.get('suggested_actions', []):
                    st.markdown(f'- {a}')
                if v['evidence']:
                    st.dataframe(v['evidence'], use_container_width=True)
                verdict = st.selectbox('Phản hồi của nhà phát triển',
                                       ['accepted_fix', 'intended', 'false_positive', 'ignore_for_now'],
                                       key=f'vd_{v["id"]}')
                note = st.text_input('Ghi chú', key=f'nt_{v["id"]}')
                if st.button('Gửi phản hồi', key=f'bt_{v["id"]}'):
                    service.submit_feedback(v['id'], verdict, note)
                    st.success('Đã ghi nhận; sẽ áp dụng ở lần phân tích sau.')
    with tab2:
        st.graphviz_chart(build_dot(report))
        st.json(report['ca_summary'])
    with tab3:
        st.code('\n'.join(report['trace']))
