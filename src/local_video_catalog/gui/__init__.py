"""画面（tkinter）.

構造の原則:

    app.py     ウィジェットの組み立てだけ。ロジックを持たない
    runner.py  別プロセス起動・出力の取り込み・停止要求（tkinter を使わない）
    state.py   画面状態の保存/復元（tkinter を使わない）

``runner`` と ``state`` は tkinter を import しない。
**画面を起動しなくても unittest で検証できることを最優先する。**
"""
