import React from "react";
import { Link } from "react-router-dom";
import { ShieldCheck } from "lucide-react";

// Public page (no sign-in needed). Google requires a privacy policy URL before
// the sign-in app can be published to everyone. Keep this in step with what
// the code actually does — see .claude/CLAUDE.md for the data flow.
const CONTACT = "paawansingal.dev@gmail.com";
const UPDATED = "27 September 2026";

function Section({ title, children }) {
  return (
    <section className="mt-8">
      <h2 className="text-lg font-semibold text-white">{title}</h2>
      <div className="mt-2 space-y-3 leading-relaxed text-slate-400">{children}</div>
    </section>
  );
}

export default function Privacy() {
  return (
    <main className="mx-auto w-full max-w-3xl flex-1 px-5 py-12 sm:px-8 sm:py-16" data-testid="privacy-page">
      <div className="glass p-6 sm:p-10">
        <span className="label-mono flex items-center gap-2">
          <ShieldCheck className="h-4 w-4" />
          Last updated {UPDATED}
        </span>
        <h1 className="mt-2 text-2xl font-bold tracking-tight text-white sm:text-3xl">
          Privacy Policy
        </h1>
        <p className="mt-4 leading-relaxed text-slate-400">
          Alphavox extracts one speaker&rsquo;s parts from a long YouTube video and can turn
          what they said into stock insights. This page explains what we collect to do that,
          and what happens to it. Questions: <a className="text-accent-soft hover:text-white" href={`mailto:${CONTACT}`}>{CONTACT}</a>.
        </p>

        <Section title="What we collect">
          <p>
            <strong className="text-slate-200">From Google sign-in:</strong> your name, email
            address, profile picture and Google account ID. We never see your Google password
            and get no access to your Gmail, Drive or any other Google data.
          </p>
          <p>
            <strong className="text-slate-200">What you give us:</strong> the YouTube links you
            submit, the speaker you pick, and the voices you save as favourites (with any name you
            give them).
          </p>
          <p>
            <strong className="text-slate-200">What we make from it:</strong> the video&rsquo;s
            title and length, the detected speakers and when each one talks, a transcript, the
            final edited video and its audio, and, if you ask for insights, a CSV of the stock
            recommendations mentioned. To find your favourite voices in new videos we compute a
            &ldquo;voice print&rdquo; for each detected speaker: a list of numbers describing how
            the voice sounds, not a recording of it.
          </p>
          <p>
            <strong className="text-slate-200">In your browser:</strong> a sign-in token kept in
            your browser&rsquo;s local storage so you stay signed in. We use no advertising or
            analytics trackers. The site loads fonts from Google Fonts, and the sign-in page loads
            Google&rsquo;s sign-in script.
          </p>
        </Section>

        <Section title="How we use it">
          <p>
            Only to run the service: to process the videos you submit, show you your own videos,
            enforce the daily usage limits, and prevent abuse. We don&rsquo;t sell your data or
            use it for advertising. Other users can&rsquo;t see your videos. The site
            administrator can, to troubleshoot problems.
          </p>
        </Section>

        <Section title="Services that process it for us">
          <ul className="list-disc space-y-1 pl-5">
            <li>Google: sign-in, and the Gemini API, which receives the final audio when you generate insights</li>
            <li>Modal: runs the video processing (speaker detection, transcription, editing)</li>
            <li>Cloudflare R2: stores the video files, transcripts and CSVs</li>
            <li>MongoDB Atlas: stores your account and your videos&rsquo; details</li>
            <li>Render and Vercel: host the app&rsquo;s server and website</li>
            <li>A proxy provider that YouTube downloads go through; it sees the download traffic but not your account</li>
          </ul>
        </Section>

        <Section title="How long we keep it">
          <p>
            Temporary processing files, including the full downloaded video and its audio, are
            deleted once processing finishes, and within about two days at most. Voice prints of
            the speakers in a video are deleted after about a week. Your finished videos,
            transcripts and CSVs, your account details, and your favourite voices (their voice
            prints and a short sample clip) are kept until you remove them or ask us to delete
            them.
          </p>
          <p>
            Download links we generate expire after one hour. Anyone you share a link with during
            that hour can download the file.
          </p>
        </Section>

        <Section title="Your choices">
          <p>
            You can sign out at any time. To have your account and all your videos deleted, email{" "}
            <a className="text-accent-soft hover:text-white" href={`mailto:${CONTACT}`}>{CONTACT}</a>{" "}
            from the address you sign in with.
          </p>
        </Section>

        <Section title="Changes">
          <p>If this policy changes, we&rsquo;ll update this page and the date at the top.</p>
        </Section>

        <p className="mt-10 text-sm">
          <Link to="/" className="text-accent-soft hover:text-white">Back to Alphavox</Link>
        </p>
      </div>
    </main>
  );
}
