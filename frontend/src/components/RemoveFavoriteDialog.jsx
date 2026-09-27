import React, { useEffect, useRef, useState } from "react";
import { AlertTriangle, Heart, Loader2 } from "lucide-react";
import { deleteFavorite, getFavorite } from "../lib/api";
import SamplePlayer from "./SamplePlayer";

/**
 * Confirm before removing a favourite voice. Plays the ORIGINAL saved sample
 * (fetched fresh — presigned links expire), so the user can check they're
 * removing the voice they think they are: a card in the favourites box can be
 * a wrong match, and removing from there deletes the saved voice itself.
 *
 * Props: favoriteId, onRemoved(favoriteId), onCancel()
 */
export default function RemoveFavoriteDialog({ favoriteId, onRemoved, onCancel }) {
  const [fav, setFav] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const cancelRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    getFavorite(favoriteId)
      .then((f) => { if (!cancelled) setFav(f); })
      .catch((err) => { if (!cancelled) setLoadError(err); });
    return () => { cancelled = true; };
  }, [favoriteId]);

  useEffect(() => {
    cancelRef.current?.focus();
    function onKey(e) {
      if (e.key === "Escape" && !busy) onCancel();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onCancel]);

  async function remove() {
    setBusy(true);
    setError(null);
    try {
      await deleteFavorite(favoriteId);
      onRemoved(favoriteId);
    } catch (err) {
      // Already gone counts as removed.
      if (err.status === 404) onRemoved(favoriteId);
      else {
        setError(err.message || "Couldn't remove this voice");
        setBusy(false);
      }
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-ink-950/75 px-4 backdrop-blur-sm"
      onClick={() => !busy && onCancel()}
      data-testid="remove-favorite-dialog"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="remove-fav-title"
        className="glass w-full max-w-md p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-rose-400/30 bg-rose-400/10 text-rose-300">
            <Heart className="h-5 w-5 fill-current" />
          </span>
          <h2 id="remove-fav-title" className="text-lg font-semibold text-white">
            Remove this voice from your favourites?
          </h2>
        </div>
        <p className="mt-3 text-sm text-slate-400">
          It won&rsquo;t be suggested in your videos any more. This is the voice you saved
          {fav && fav.name ? <> as <span className="text-slate-200">{fav.name}</span></> : null}:
        </p>

        <div className="mt-4 rounded-xl border border-white/[0.06] bg-ink-950/60 p-3">
          {fav ? (
            <SamplePlayer src={fav.sample_url} testId="remove-favorite-sample" />
          ) : loadError ? (
            <p className="text-center font-mono text-xs text-slate-500">Couldn&rsquo;t load the saved sample</p>
          ) : (
            <p className="flex items-center justify-center gap-2 font-mono text-xs text-slate-500">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading the saved sample…
            </p>
          )}
        </div>
        {fav && fav.source_video_title && (
          <p className="mt-2 truncate font-mono text-[11px] text-slate-600" title={fav.source_video_title}>
            from: {fav.source_video_title}
          </p>
        )}

        {error && (
          <p className="mt-4 flex items-center gap-2 rounded-xl border border-bear/30 bg-bear/10 px-3 py-2 text-sm text-bear-soft">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            {error}
          </p>
        )}

        <div className="mt-6 flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          <button ref={cancelRef} type="button" className="btn-ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            onClick={remove}
            disabled={busy}
            className="inline-flex items-center justify-center gap-2 rounded-xl border border-bear/40 bg-bear/15 px-4 py-2.5 text-sm font-semibold text-bear-soft transition-colors hover:bg-bear/25 disabled:opacity-50"
            data-testid="remove-favorite-confirm"
          >
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}
            Remove from favourites
          </button>
        </div>
      </div>
    </div>
  );
}
