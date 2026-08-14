"""各工程を台帳へ結びつける薄い層.

**新しい解析ロジックをここへ書かない。** 判定規則は ``database``（再利用
キー）と ``pipeline``（止めどき）に既にある。各工程がすることは:

    1. 前提を確かめる
    2. 再利用できるなら再利用する
    3. 部品を呼ぶ
    4. 結果を台帳へ書く
    5. ``StageOutcome`` を返す

**失敗を completed にしない。** 途中で失敗したら、そこまでの成果だけを
残して失敗として返す。次回は残りだけをやり直す。
"""

from .description import run_description
from .frames import run_frame_extraction
from .transcription import run_transcription
from .visual import run_visual_analysis

__all__ = [
    "run_frame_extraction",
    "run_visual_analysis",
    "run_transcription",
    "run_description",
]
