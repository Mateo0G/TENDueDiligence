from app.tasks import noop  # noqa: F401  (import-for-registration side effect)
from app.tasks import digest_document  # noqa: F401
from app.tasks import read_files  # noqa: F401
from app.tasks import key_facts  # noqa: F401
from app.tasks import research  # noqa: F401
from app.tasks import warm_cache  # noqa: F401
from app.tasks import draft_section  # noqa: F401
from app.tasks import compile_draft  # noqa: F401
from app.tasks import qa_score  # noqa: F401
from app.tasks import qa_all  # noqa: F401
from app.tasks import report_card  # noqa: F401
from app.tasks import gap_analysis  # noqa: F401
from app.tasks import mitigation_strategy  # noqa: F401
from app.tasks import final_render  # noqa: F401
