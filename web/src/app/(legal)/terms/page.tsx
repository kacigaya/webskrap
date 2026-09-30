import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Terms of use",
  description: "Terms for the WebSkrap website, documentation, and software.",
  alternates: { canonical: "https://kacigaya.github.io/webskrap/terms/" },
  openGraph: {
    title: "Terms of use | WebSkrap",
    description: "Terms for the WebSkrap website, documentation, and software.",
    type: "website",
  },
  twitter: {
    card: "summary",
    title: "Terms of use | WebSkrap",
    description: "Terms for the WebSkrap website, documentation, and software.",
  },
};

export default function TermsPage() {
  return (
    <>
      <h1>Terms of use</h1>
      <p className="text-xs text-muted-foreground tabular-nums">
        Last updated <time dateTime="2026-09-30">30 September 2026</time>
      </p>
      <h2>Scope</h2>
      <p>
        These terms cover this website and its documentation. The WebSkrap
        software is governed by its license, described below. Using the site
        means you accept these terms.
      </p>
      <h2>Software license</h2>
      <p>
        WebSkrap is open-source software released under the{" "}
        <a href="https://github.com/kacigaya/webskrap/blob/main/LICENSE">
          Apache License 2.0
        </a>
        . The license alone sets your rights to use, modify, and redistribute
        the code, and nothing here narrows or extends it. The documentation and
        site source live in the same repository under the same license unless a
        file says otherwise.
      </p>
      <h2>Responsible use</h2>
      <p>
        WebSkrap automates a real browser. You alone decide which sites you
        visit with it and what you do with the data. You are responsible for
        complying with the laws and agreements that apply to you, including:
      </p>
      <ul>
        <li>
          the terms of service, robots.txt rules, and rate limits of the sites
          you access;
        </li>
        <li>
          data protection law, such as the GDPR, when collected data identifies
          people;
        </li>
        <li>copyright, database rights, and other intellectual property;</li>
        <li>laws against unauthorized access to computer systems.</li>
      </ul>
      <p>
        Do not use WebSkrap to access accounts, paywalled content, or systems
        you are not authorized to use, to defeat authentication or access
        controls, or to overload a service. Its stealth features exist to make
        automation behave like an ordinary browser for testing, research, and
        permitted collection. WebSkrap does not solve CAPTCHAs, and the
        maintainer does not endorse using it to evade a site’s decision to
        refuse automated traffic.
      </p>
      <h2>Benchmarks and documentation</h2>
      <p>
        Benchmarks and detection results are dated snapshots from specific
        hosts, networks, and browser versions. Detection services and browsers
        change, so a published result does not guarantee future behavior.
        Documentation is provided for information and may lag the latest
        release.
      </p>
      <h2>No warranty</h2>
      <p>
        The site, documentation, and software are provided “as is”, without
        warranties of any kind. To the extent the law allows, the operator is
        not liable for damages arising from their use, including account
        suspensions, blocked access, or claims by third parties. Nothing here
        limits liability that cannot be limited by law, or rights you hold as a
        consumer.
      </p>
      <h2>Trademarks</h2>
      <p>
        Third-party names on this site, such as Cloudflare, Google reCAPTCHA,
        Playwright, Chrome, GitHub, and PyPI, are trademarks of their owners.
        They are mentioned to describe compatibility or test results. WebSkrap
        is not affiliated with or endorsed by them.
      </p>
      <h2>Governing law</h2>
      <p>
        These terms are governed by French law. If you are a consumer, you also
        keep the protection of the mandatory rules of your country of
        residence.
      </p>
      <h2>Changes and contact</h2>
      <p>
        These terms may change; the date above identifies the latest revision.
        Questions go to{" "}
        <a href="mailto:contact@gaya.anonaddy.com">contact@gaya.anonaddy.com</a>
        .
      </p>
      <p>
        <Link href="/legal-notice">Legal notice</Link> ·{" "}
        <Link href="/privacy">Privacy policy</Link> ·{" "}
        <Link href="/">Back to WebSkrap</Link>
      </p>
    </>
  );
}
