"""元動画の位置の表し方（APP_ROOT の外にある外部入力）.

**内部生成物のパスと元動画のパスを取り違えないための境界。**

内部生成物（cache・descriptions・catalog・logs）は APP_ROOT 配下にあり、
``paths.to_app_relative`` で **APP_ROOT からの相対**として台帳へ保存する。
フォルダーごと移動しても追随させるためである。

元動画はまったく別物である。

  - APP_ROOT の外にある
  - アプリの持ち物ではない。**読むだけ**で、書き換え・改名・移動・削除を
    一切しない
  - アプリのフォルダーを移動しても、元動画は元の場所にあり続ける

したがって元動画を APP_ROOT 相対で保存してはいけない。意味が違うものを
同じ形で持つと、いつか片方の規則をもう片方へ適用してしまう。

このモジュールは元動画を

    「解析対象フォルダー（source root）」+「その中での相対パス」

の 2 つ組で表す。source root は利用者が指定した外部の絶対パスで、
相対部分はその中での位置である。こう分けておくと、

  - 外部入力であることが型の上で明らかになる
  - 元動画一式を別ドライブへ移した利用者が、source root だけを
    指し直せば済む（v1 では自動追随まではしない）
  - ログや台帳の表示で、どこまでが利用者の領域かが分かる

v1 では source root は 1 つだけを想定する。複数対応は入れない。
ただし**複数になっても壊れない形**にしておく（root を各行が持つ）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class SourceRefError(Exception):
    """元動画の位置を表せない。推測で補わずに停止する。"""


@dataclass(frozen=True)
class SourceRef:
    """元動画 1 件の位置。

    Attributes:
        root: 解析対象フォルダーの絶対パス（APP_ROOT の外・利用者の領域）
        relative: root からの相対パス（POSIX 表記）
    """

    root: Path
    relative: str

    def __post_init__(self) -> None:
        if not str(self.relative).strip():
            raise SourceRefError("relative が空です。")
        if Path(self.relative).is_absolute():
            raise SourceRefError(
                f"relative に絶対パスを入れないでください: {self.relative}"
            )
        if ".." in Path(self.relative).parts:
            raise SourceRefError(
                f"relative に '..' を含めないでください: {self.relative}"
            )

    # -- 導出 -------------------------------------------------------------

    @property
    def absolute(self) -> Path:
        """実際に開くときのパス。**読み取り専用でのみ使う。**"""
        return self.root / Path(self.relative)

    @property
    def file_name(self) -> str:
        return Path(self.relative).name

    @property
    def extension(self) -> str:
        return Path(self.relative).suffix.lower()

    def parent_names(self, limit: int = 3) -> list[str]:
        """相対パス側の親フォルダー名（近い順）。

        撮影時期の手がかりを探すのに使う。**source root の外側の
        フォルダー名は返さない**（利用者の領域の構成を必要以上に
        持ち出さないため）。
        """
        names = [p.name for p in Path(self.relative).parents if p.name]
        return names[:limit] if limit is not None else names

    # -- 変換 -------------------------------------------------------------

    def to_row(self) -> dict[str, str]:
        """台帳へ保存する形。"""
        return {"source_root": str(self.root), "source_relative": self.relative}

    @classmethod
    def from_row(cls, row: object) -> "SourceRef":
        """台帳の行から復元する。``sqlite3.Row`` でも dict でも動く。"""
        try:
            root = row["source_root"]
            relative = row["source_relative"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SourceRefError(
                "source_root / source_relative がありません。"
            ) from exc
        if not root or not relative:
            raise SourceRefError("source_root / source_relative が空です。")
        return cls(root=Path(root), relative=str(relative))

    @classmethod
    def from_absolute(cls, path: Path | str, root: Path | str) -> "SourceRef":
        """絶対パスと source root から作る。

        Raises:
            SourceRefError: path が root の配下にない場合。
                **推測で root を作り直さない。** 想定外の場所の動画を
                黙って取り込むと、利用者が指定していないフォルダーを
                解析することになる。
        """
        resolved_root = Path(root).resolve()
        try:
            relative = Path(path).resolve().relative_to(resolved_root)
        except (ValueError, OSError) as exc:
            raise SourceRefError(
                f"解析対象フォルダーの外にあります。\n"
                f"  対象フォルダー: {resolved_root}\n"
                f"  ファイル      : {path}"
            ) from exc
        return cls(root=resolved_root, relative=relative.as_posix())

    def __str__(self) -> str:
        return str(self.absolute)


def is_inside(path: Path | str, root: Path | str) -> bool:
    """path が root の配下にあるか。判定は ``resolve()`` 後に行う。"""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False
    return True
