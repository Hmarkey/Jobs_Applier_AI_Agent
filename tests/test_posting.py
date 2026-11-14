"""A job added from its link: read as the job database has it, else off its page, and kept among the jobs found."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from aihawk import posting
from aihawk.database import Job, load_manifest
from aihawk.library import Library
from aihawk.posting import PostingError, check_link, from_structured, from_text, page_text, read_link
from aihawk.store import Store
from fakes import ScriptedModel, publish

TEXT = "Build and ship the models behind our product. " * 8
GREENHOUSE = f"""<html><head><title>Job Application for ML Engineer at Acme</title>
<script type="application/ld+json">
{json.dumps({"@context": "https://schema.org", "@type": "JobPosting", "title": "Machine Learning Engineer",
             "hiringOrganization": {"@type": "Organization", "name": "Acme &amp; Co"},
             "jobLocation": [{"@type": "Place", "address": {"addressLocality": "New York", "addressRegion": "NY", "addressCountry": "US"}}],
             "jobLocationType": "TELECOMMUTE", "description": f"<p>{TEXT}</p><ul><li>Python</li></ul>"})}
</script></head><body><div id="app"></div></body></html>"""


def test_a_link_must_be_to_a_web_page() -> None:
    assert check_link("  https://jobs.lever.co/acme/1  ") == "https://jobs.lever.co/acme/1"
    for bad in ("", "jobs.lever.co/acme/1", "ftp://x.example/1", "https://localhost/1", "javascript:alert(1)"):
        with pytest.raises(PostingError, match="链接"):
            check_link(bad)


def test_the_posting_a_page_describes_for_search_engines_is_read() -> None:
    found = from_structured(GREENHOUSE)
    assert found is not None
    assert (found["title"], found["company"], found["location"]) == ("Machine Learning Engineer", "Acme & Co", "Remote; New York, NY, US")
    assert TEXT.strip() in found["description"] and "• Python" in found["description"] and "<" not in found["description"]


def test_a_posting_inside_a_graph_or_a_list_is_found_and_a_broken_block_skipped() -> None:
    graph = {"@graph": [{"@type": "WebSite"}, {"@type": ["JobPosting"], "title": "Data Scientist", "hiringOrganization": "Beta",
                                                "jobLocation": {"address": "Austin, TX"}}]}
    page = f'<script type="application/ld+json">{{not json</script><script type="application/ld+json">{json.dumps([graph])}</script>'
    assert from_structured(page) == {"title": "Data Scientist", "company": "Beta", "location": "Austin, TX", "description": ""}
    assert from_structured("<html><body>No data here</body></html>") is None
    # A posting with no company says too little to be a job.
    assert from_structured('<script type="application/ld+json">{"@type": "JobPosting", "title": "X"}</script>') is None


def test_what_a_model_is_shown_of_a_page_is_its_title_and_text_without_scripts() -> None:
    text = page_text('<html><head><title>Senior Engineer - Acme</title><meta property="og:site_name" content="Acme Careers">'
                     "<script>var secret = 1;</script></head><body><h1>Senior Engineer</h1><p>Do things.</p></body></html>")
    assert text.startswith("Senior Engineer - Acme\nAcme Careers") and "Do things." in text and "secret" not in text


def test_the_model_reads_a_page_with_no_posting_data_and_says_when_it_is_not_one() -> None:
    page = f"<html><body><h1>Applied Scientist</h1><p>{TEXT}</p></body></html>"
    model = ScriptedModel(json.dumps({"is_posting": True, "company": " Gamma ", "title": "Applied  Scientist", "location": "Remote", "description": TEXT}))
    found = asyncio.run(from_text(model, "m", "https://gamma.example/jobs/1", page))  # type: ignore[arg-type]
    assert found == {"company": "Gamma", "title": "Applied Scientist", "location": "Remote", "description": TEXT.strip()}
    assert "https://gamma.example/jobs/1" in model.requests[0]["messages"][1]["content"]
    model = ScriptedModel(json.dumps({"is_posting": False, "company": "", "title": "", "location": "", "description": ""}))
    assert asyncio.run(from_text(model, "m", "https://gamma.example/jobs", page)) is None  # type: ignore[arg-type]
    # A page with no text of its own (one a browser fills in) is not even shown to the model.
    assert asyncio.run(from_text(ScriptedModel(), "m", "https://x.example/", '<div id="root"></div>')) is None  # type: ignore[arg-type]


def library_with(tmp_path: Path, url: str) -> Library:
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t (key VARCHAR, url VARCHAR, title VARCHAR, company VARCHAR, location VARCHAR, description VARCHAR, posted_at VARCHAR)")
    connection.execute("INSERT INTO t VALUES ('k', ?, 'Research Engineer', 'Delta', 'Boston', ?, NULL)", [url, TEXT])
    location = publish(connection, tmp_path, 1)
    library = Library(tmp_path / "library")
    library.sync(load_manifest(location), location)
    return library


def test_the_job_database_knows_a_posting_under_its_link_or_another_of_its_links(tmp_path: Path) -> None:
    library = library_with(tmp_path, "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Boston/Research-Engineer_R123")
    for url in ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/Boston/Research-Engineer_R123",
                "https://acme.wd1.myworkdayjobs.com/careers/job/Research-Engineer_R123/apply?source=LinkedIn"):
        known = library.posting(url)
        assert known is not None and known["company"] == "Delta" and known["url"].endswith("_R123"), url
    assert library.posting("https://acme.wd1.myworkdayjobs.com/careers/job/Other_R999") is None
    assert Library(tmp_path / "empty").posting("https://x.example/1") is None


def test_a_link_the_database_knows_is_not_fetched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://jobs.lever.co/delta/42"
    monkeypatch.setattr(posting, "fetch", lambda url: pytest.fail("fetched"))
    job = asyncio.run(read_link(url + "/apply", library_with(tmp_path, url), ScriptedModel(), "m"))  # type: ignore[arg-type]
    assert job == Job(url, "Delta", "Research Engineer", "Boston", url, url, TEXT, posting.CHOSEN)


def test_a_link_it_does_not_know_is_read_off_its_page_at_the_address_it_ends_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    landed = "https://job-boards.greenhouse.io/acme/jobs/7"
    monkeypatch.setattr(posting, "fetch", lambda url: (landed, GREENHOUSE))
    job = asyncio.run(read_link("https://go.short.example/x", Library(tmp_path / "none"), ScriptedModel(), "m"))  # type: ignore[arg-type]
    assert (job.id, job.url, job.apply_url, job.company, job.title) == (landed, landed, landed, "Acme & Co", "Machine Learning Engineer")


def test_a_page_with_no_job_on_it_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(posting, "fetch", lambda url: (url, "<html><body><p>Sign in to continue.</p></body></html>"))
    with pytest.raises(PostingError, match="读不出职位信息"):
        asyncio.run(read_link("https://x.example/jobs/1", Library(tmp_path / "none"), ScriptedModel(), "m"))  # type: ignore[arg-type]


def test_a_page_that_does_not_open_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: Any, timeout: float) -> Any:
        import urllib.error

        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(posting.urllib.request, "urlopen", refuse)
    with pytest.raises(PostingError, match="404，职位可能已经关闭"):
        posting.fetch("https://x.example/jobs/1")


def test_the_store_knows_a_job_under_another_of_its_links(tmp_path: Path) -> None:
    store = Store(tmp_path / "jobs.db")
    url = "https://jobs.lever.co/acme/1"
    store.add(Job(url, "Acme", "Engineer", "Remote", url, url, "d"))
    assert store.known(url + "/apply?utm_source=x") is not None and store.known(url + "/apply").job.id == url  # type: ignore[union-attr]
    assert store.known("https://jobs.lever.co/acme/2") is None
    store.close()
