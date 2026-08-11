// The About page: what this thing is, where the docs are, and how to reach a
// human. Deliberately short — a reader who opens About wants three facts, not a
// pitch, and this repo's copy rules forbid hype vocabulary anywhere on camera.
//
// External links open with window.open(..., "_blank") rather than a bare <a
// target>, matching Settings.jsx's releases link: the board runs under a strict
// CSP and this is the pattern already proven to work here. The contact address
// is a mailto: — no JS needed, and it degrades to the OS mail client.

const DOCS_URL = "https://getnohuman.com/docs";
const CONTACT_EMAIL = "hello@getnohuman.com";

function openDocs() {
  window.open(DOCS_URL, "_blank", "noopener,noreferrer");
}

export default function About() {
  return (
    <div className="nh-about">
      <section className="nh-about-block">
        <h2>What no_human is</h2>
        <p>
          no_human turns a ticket into a reviewed pull request. It plans the change,
          writes it, runs your test suite, and puts an independent reviewer in front of
          the result before anything reaches you.
        </p>
        <p>
          It never merges. Every task ends at a PR you approve, and it pushes only to its
          own branches — never to <code>main</code>. When it cannot finish honestly it
          stops and says why, rather than reporting success it did not earn.
        </p>
        <p>
          It runs on your machine, on your own Claude credentials, against your own
          repositories.
        </p>
      </section>

      <section className="nh-about-block">
        <h2>Documentation</h2>
        <p>
          Setup, configuration and the task lifecycle are documented on the site.
        </p>
        <button type="button" className="btn" onClick={openDocs}>
          Open the docs
        </button>
      </section>

      <section className="nh-about-block">
        <h2>Contact</h2>
        <p>
          Questions, bugs, or something it got wrong — mail{" "}
          <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.
        </p>
      </section>
    </div>
  );
}
