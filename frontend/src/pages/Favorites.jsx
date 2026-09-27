import React, { useCallback, useEffect, useState } from "react";
import { motion } from "framer-motion";
import { AlertTriangle, Check, Heart, Loader2, RefreshCw, Trash2 } from "lucide-react";
import { listFavorites, renameFavorite } from "../lib/api";
import RemoveFavoriteDialog from "../components/RemoveFavoriteDialog";
import SamplePlayer from "../components/SamplePlayer";

const MAX_FAVORITES = 20;
const NAME_MAX = 60;

// Mongo returns naive UTC datetimes; read them as UTC (see MyJobs.jsx).
function addedOn(iso) {
  if (!iso) return "";
  const s = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`;
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-IN", {
    timeZone: "Asia/Kolkata", day: "numeric", month: "short", year: "numeric",
  });
}

/**
 * One saved voice: listen, name it (the name is entirely the user's; nothing
 * is pre-filled), or remove it.
 */
function FavoriteRow({ fav, onRenamed, onRemove, index }) {
  const [name, setName] = useState(fav.name || "");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(null);

  async function save() {
    const next = name.trim();
    if (next === (fav.name || "")) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await renameFavorite(fav.favorite_id, next);
      onRenamed(updated);
      setName(updated.name || "");
      setSaved(true);
      setTimeout(() => setSaved(false), 1600);
    } catch (err) {
      setError(err.message || "Couldn't save the name");
    } finally {
      setSaving(false);
    }
  }

  return (
    <motion.li
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, delay: Math.min(index * 0.03, 0.3), ease: [0.22, 1, 0.36, 1] }}
      className="glass flex flex-col gap-4 p-5"
      data-testid="favorite-row"
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <label className="sr-only" htmlFor={`fav-name-${fav.favorite_id}`}>
            Name for this voice
          </label>
          <div className="relative">
            <input
              id={`fav-name-${fav.favorite_id}`}
              type="text"
              value={name}
              maxLength={NAME_MAX}
              placeholder="Add a name"
              onChange={(e) => setName(e.target.value)}
              onBlur={save}
              onKeyDown={(e) => {
                if (e.key === "Enter") e.currentTarget.blur();
              }}
              className="w-full rounded-xl border border-white/10 bg-ink-900/80 px-3 py-2.5 pr-9 text-sm font-semibold text-white outline-none transition-all duration-200 placeholder:font-normal placeholder:text-slate-600 focus:border-accent/60 focus:ring-4 focus:ring-accent/15"
              data-testid="favorite-name-input"
            />
            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2">
              {saving ? (
                <Loader2 className="h-4 w-4 animate-spin text-slate-500" />
              ) : saved ? (
                <Check className="h-4 w-4 text-bull" />
              ) : null}
            </span>
          </div>
          <p className="mt-2 truncate font-mono text-[11px] text-slate-500" title={fav.source_video_title || ""}>
            Added {addedOn(fav.created_at)}
            {fav.source_video_title ? ` · from: ${fav.source_video_title}` : ""}
          </p>
          {error && <p className="mt-1 text-xs text-bear-soft">{error}</p>}
        </div>
        <button
          type="button"
          onClick={() => onRemove(fav.favorite_id)}
          aria-label="Remove from favourites"
          title="Remove from favourites"
          className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-white/10 bg-white/[0.03] text-slate-400 transition-all duration-200 hover:border-bear/40 hover:bg-bear/10 hover:text-bear-soft"
          data-testid="favorite-remove-btn"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </div>
      <div className="rounded-xl border border-white/[0.06] bg-ink-950/60 p-3">
        <SamplePlayer src={fav.sample_url} testId="favorite-sample" />
      </div>
    </motion.li>
  );
}

export default function Favorites() {
  const [favs, setFavs] = useState(null); // null = not loaded yet
  const [loadError, setLoadError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [removing, setRemoving] = useState(null);

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const data = await listFavorites();
      setFavs(Array.isArray(data) ? data : []);
      setLoadError(null);
    } catch (err) {
      setLoadError(err);
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function onRenamed(updated) {
    setFavs((prev) => prev.map((f) => (f.favorite_id === updated.favorite_id ? { ...f, ...updated } : f)));
  }

  function onRemoved(favoriteId) {
    setFavs((prev) => prev.filter((f) => f.favorite_id !== favoriteId));
    setRemoving(null);
  }

  if (favs === null && !loadError) {
    return (
      <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 text-slate-500" data-testid="favorites-loading">
        <Loader2 className="h-7 w-7 animate-spin text-accent-soft" />
        <span className="font-mono text-sm">Loading your favourite voices…</span>
      </div>
    );
  }

  if (loadError && favs === null) {
    return (
      <main className="mx-auto w-full max-w-5xl flex-1 px-5 py-12 sm:px-8" data-testid="favorites-error">
        <div className="glass mx-auto max-w-xl p-8 text-center">
          <span className="mx-auto mb-5 grid h-14 w-14 place-items-center rounded-2xl border border-bear/30 bg-bear/10">
            <AlertTriangle className="h-7 w-7 text-bear" />
          </span>
          <h2 className="text-2xl font-bold text-white">Couldn&rsquo;t load your favourite voices</h2>
          <p className="mt-3 text-slate-400">{loadError.message}</p>
          <button type="button" className="btn-ghost mx-auto mt-6" onClick={load}>
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
            Try again
          </button>
        </div>
      </main>
    );
  }

  return (
    <main className="mx-auto w-full max-w-5xl flex-1 px-5 py-10 sm:px-8 sm:py-14" data-testid="favorites-page">
      <div className="mb-7 flex items-end justify-between gap-4">
        <div>
          <span className="label-mono flex items-center gap-2">
            <Heart className="h-4 w-4" />
            Favourite voices
          </span>
          <h1 className="mt-2 text-2xl font-bold tracking-tight text-white sm:text-3xl">
            Your favourite voices
          </h1>
          <p className="mt-2 max-w-2xl text-sm text-slate-400">
            When one of these voices is in a new video, it shows up first on the speaker page.
            {favs.length > 0 && ` ${favs.length} of ${MAX_FAVORITES} saved.`}
          </p>
        </div>
        <button type="button" className="btn-ghost shrink-0" onClick={load} disabled={refreshing}>
          <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {favs.length === 0 ? (
        <div className="glass mx-auto max-w-xl p-8 text-center" data-testid="favorites-empty">
          <span className="mx-auto mb-5 grid h-14 w-14 place-items-center rounded-2xl border border-rose-400/25 bg-rose-400/10">
            <Heart className="h-7 w-7 text-rose-300" />
          </span>
          <h2 className="text-2xl font-bold text-white">No favourite voices yet</h2>
          <p className="mt-3 text-slate-400">
            While you&rsquo;re choosing a speaker, tap the heart on a voice to save it. Next time
            that voice is in a video, it&rsquo;ll be waiting at the top.
          </p>
        </div>
      ) : (
        <ul className="grid grid-cols-1 gap-4 md:grid-cols-2" data-testid="favorites-list">
          {favs.map((fav, i) => (
            <FavoriteRow
              key={fav.favorite_id}
              fav={fav}
              index={i}
              onRenamed={onRenamed}
              onRemove={setRemoving}
            />
          ))}
        </ul>
      )}

      {removing && (
        <RemoveFavoriteDialog
          favoriteId={removing}
          onRemoved={onRemoved}
          onCancel={() => setRemoving(null)}
        />
      )}
    </main>
  );
}
