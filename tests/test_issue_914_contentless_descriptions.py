"""Issue #914: boilerplate that names no subject is not a Content Description.

The #868 whole-clause check knew «…в названии» but not «…в заглавии «Title»»,
nor a clause that only promises an account of "the stated subject". A
description that says the capture held only a title and author passed too.
Both texts below reached the 2026-11-02 review.
"""

from __future__ import annotations

import pytest

from autoscout.content_description import validate_content_description

TEMPLATE_REASON = "contentless title/rank template rejected"
ADMISSION_REASON = "description admits it has no book content"

MAQUINA = (
    "Книга посвящена теме, обозначенной в заглавии «La máquina infinita». "
    "Автор предлагает научно-популярный обзор заявленного предмета."
)
MURAKAMI = (
    "Книга «Abandoning a Cat: A Personal Story» указана в строке BookMarks; автор — Haruki Murakami, "
    "trans. by Philip Gabriel. В захвате зафиксированы только название и автор, поэтому тематический угол "
    "и редакционный хук не раскрыты."
)
REAL = (
    "Историк восстанавливает, как в портовых городах Средиземноморья складывались первые страховые общества. "
    "Автор опирается на нотариальные записи и переписку торговых домов."
)


@pytest.mark.parametrize(
    "text",
    [
        MAQUINA,
        "Книга посвящена теме, обозначенной в заглавии.",
        "Книга о теме, указанной в заглавии «Mr. Smith. A Life».",
        "Тема книги отражена в заглавии «Infinity».",
        "Книга посвящена теме, обозначенной в названии “The Long Run”.",
        "Автор предлагает обзор заявленной темы.",
        "Книга даёт краткий разбор указанного вопроса.",
        "Позиция 14 в рейтинге; автор предлагает научно-популярный обзор заявленного предмета.",
    ],
)
def test_title_and_stated_subject_boilerplate_is_rejected(text: str) -> None:
    assert validate_content_description(text) == (None, TEMPLATE_REASON)


@pytest.mark.parametrize(
    "text",
    [
        MURAKAMI,
        "О книге известно только название и автор, поэтому её содержание здесь не описано.",
        "Мемуары актёра. В источнике указаны только название и автор.",
        "В строке BookMarks указаны только название и автор книги, содержание неизвестно.",
    ],
)
def test_a_description_that_admits_it_has_no_content_is_rejected(text: str) -> None:
    assert validate_content_description(text) == (None, ADMISSION_REASON)


@pytest.mark.parametrize(
    "text",
    [
        REAL,
        f"Книга посвящена теме, обозначенной в заглавии «Страховщики». {REAL}",
        "Тема, обозначенная в заглавии, раскрывается через историю городского расселения и архивы банков.",
        "Автор предлагает обзор истории морского страхования от Генуи до Лондона.",
        "Автор показывает, что название книги взято из письма генуэзского купца. " + REAL,
        "Книга «Только название» — роман-эссе о том, как имя вещи меняет её восприятие у читателей.",
        # A book about a lost work whose title alone survives is not an admission (review of #914).
        "Историк исследует утраченные античные сочинения, от которых известно только название. "
        "По архивным каталогам автор восстанавливает пути передачи знаний между библиотеками.",
        "Историк исследует утраченные античные сочинения, от которых известны только название и автор. "
        "По архивным каталогам он восстанавливает пути передачи знаний между библиотеками.",
        "Историк рассказывает о книге, от которой известно только название, "
        "и реконструирует её роль в античной науке по поздним цитатам.",
        "Историк рассказывает о книге которой известно только название и реконструирует её роль по цитатам.",
        "Историк описывает первые страховые общества. "
        "Редакционный хук — не раскрытая ранее роль женщин в создании страховых обществ.",
        "Автор расследует убийство журналистки. Редакционный хук — почему до сих пор не раскрыто это преступление.",
    ],
)
def test_a_substantive_description_that_mentions_the_title_still_passes(text: str) -> None:
    validated, reason = validate_content_description(text)
    assert reason is None and validated == text
