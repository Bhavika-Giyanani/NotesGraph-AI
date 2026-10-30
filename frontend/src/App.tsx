import { type Dispatch, type FormEvent, type ReactNode, type SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownToLine, ArrowRight, AudioLines, BookOpen, Check, ChevronDown, Clock3,
  Eye, EyeOff, FileAudio2, FileText, FolderClosed, FolderOpen, LayoutDashboard,
  LoaderCircle, LogOut, Menu, MessageCircle, Moon, Plus, Search, Send, Sparkles,
  Sun, Trash2, UploadCloud, X,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { Link, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import type { Session } from "@supabase/supabase-js";
import { supabase, supabaseConfigError } from "./supabase";

type Lecture = {
  id: string; title: string; subject: string; folder_id: string | null; source: string;
  status: string; progress: string; error: string; created_at: string; notes?: Notes;
};
type Notes = {
  summary: string;
  core_explanations: { topic: string; explanation: string; key_points: string[] }[];
  announcements: { text: string; deadline: string }[];
  exam_points: { point: string; kind: string }[];
  off_topic: { summary: string }[];
};
type Folder = { id: string; name: string };
type ChatSession = { id: string; lecture_id: string; title: string; updated_at: string };
type ChatMessage = {
  id: number; role: "user" | "assistant"; content: string; created_at: string;
  status?: "pending" | "complete" | "error";
  sources?: { timestamp?: string; text: string }[];
};
const configuredApiUrl = (
  window.NOTESGRAPH_CONFIG?.apiUrl || import.meta.env.VITE_API_URL || ""
).trim();
const apiBaseError = (() => {
  if (!configuredApiUrl) return "";
  try {
    const url = new URL(configuredApiUrl);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
        (url.pathname !== "/" && url.pathname !== "") || url.search || url.hash) {
      return "VITE_API_URL must be the backend origin only, such as http://localhost:8000. Do not put an API token or path there.";
    }
    return "";
  } catch {
    return "VITE_API_URL must be a backend URL such as http://localhost:8000. Leave it empty when using the local Vite proxy.";
  }
})();
const apiBase = configuredApiUrl && !apiBaseError
  ? new URL(configuredApiUrl).origin
  : "";

function apiUrl(path: string) {
  if (apiBaseError) throw new Error(apiBaseError);
  return `${apiBase}/api${path}`;
}

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!supabase) throw new Error(supabaseConfigError);
  if (apiBaseError) throw new Error(apiBaseError);
  const { data: { session } } = await supabase.auth.getSession();
  if (!session?.access_token) throw new Error("Your session expired. Please sign in again.");
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${session.access_token}`);
  if (!(init.body instanceof FormData) && init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      headers,
      signal: init.signal || AbortSignal.timeout(
        init.body instanceof FormData || path.endsWith("/chat") ? 180_000 : 30_000,
      ),
    });
  } catch (reason) {
    if (reason instanceof DOMException && reason.name === "TimeoutError") {
      throw new Error(init.body instanceof FormData
        ? "The upload request timed out. Check that FastAPI is running and the Supabase Storage bucket is configured, then try again."
        : "The backend did not respond in time. Check that FastAPI is running on port 8000.");
    }
    if (reason instanceof TypeError) {
      throw new Error("Cannot reach the backend. Start FastAPI from the project root with: python -m uvicorn backend.app.main:app --reload --port 8000");
    }
    throw reason;
  }
  const contentType = response.headers.get("content-type") || "";
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    if (contentType.includes("application/json")) {
      try { detail = (await response.json()).detail || detail; } catch { /* Invalid JSON error body. */ }
    } else if (contentType.includes("text/html")) {
      detail = "The API returned an HTML page instead of JSON. Check VITE_API_URL and make sure the FastAPI backend is running on port 8000.";
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  if (!contentType.includes("application/json")) {
    throw new Error("The API returned a non-JSON response. Check VITE_API_URL and make sure the FastAPI backend is running.");
  }
  return response.json() as Promise<T>;
}

function useTheme() {
  const [theme, setTheme] = useState<"light" | "dark">(
    () => (localStorage.getItem("notesgraph-theme") as "light" | "dark") || "light",
  );
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("notesgraph-theme", theme);
  }, [theme]);
  return { theme, toggle: () => setTheme(theme === "light" ? "dark" : "light") };
}

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const theme = useTheme();
  useEffect(() => {
    if (!supabase) { setAuthLoading(false); return; }
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setAuthLoading(false);
    });
    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, next) => setSession(next));
    return () => subscription.unsubscribe();
  }, []);

  return (
    <Routes>
      <Route path="/" element={<Landing theme={theme} />} />
      <Route path="/sign-in" element={session ? <Navigate to="/app" replace /> : <SignIn />} />
      <Route path="/app/*" element={
        authLoading ? <LoadingScreen /> : session ? <Dashboard session={session} theme={theme} /> : <Navigate to="/sign-in" replace />
      } />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function Brand({ compact = false }: { compact?: boolean }) {
  return <Link to="/" className="brand-lockup" aria-label="NotesGraph home">
    <span className="brand-mark"><AudioLines size={24} strokeWidth={2.2} /></span>
    {!compact && <span className="brand-name"><span>Notes</span><span className="brand-accent">Graph</span></span>}
  </Link>;
}

function ThemeButton({ theme, toggle }: { theme: ReturnType<typeof useTheme>["theme"]; toggle: () => void }) {
  return <button className="icon-btn theme-toggle" aria-label={`Switch to ${theme === "light" ? "dark" : "light"} mode`} onClick={toggle}>
    {theme === "light" ? <Moon size={18} /> : <Sun size={18} />}
  </button>;
}

function Landing({ theme }: { theme: ReturnType<typeof useTheme> }) {
  const [splash, setSplash] = useState(!sessionStorage.getItem("notesgraph-splashed"));
  useEffect(() => {
    if (!splash) return;
    const timeout = window.setTimeout(() => {
      sessionStorage.setItem("notesgraph-splashed", "true");
      setSplash(false);
    }, 1350);
    return () => window.clearTimeout(timeout);
  }, [splash]);
  return (
    <main className="landing">
      <AnimatePresence mode="wait">
        {splash ? <motion.div key="splash" className="splash" initial={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: .45 }}>
          <motion.div className="splash-logo" initial={{ scale: .8, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ duration: .6, ease: "easeOut" }}>
            <Brand /><span className="splash-orbit" />
          </motion.div>
        </motion.div> : <motion.div key="home" className="landing-page" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .55 }}>
          <header className="site-nav shell">
            <Brand />
            <nav><a href="#how">How it works</a><a href="#features">Features</a></nav>
            <div className="nav-actions"><ThemeButton {...theme} /><Link className="nav-login" to="/sign-in">Sign in</Link><Link className="button button-dark" to="/sign-in">Get started <ArrowRight size={16} /></Link></div>
          </header>
          <section className="hero shell">
            <motion.div className="hero-copy" initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: .12 }}>
              <span className="eyebrow"><span className="eyebrow-dot" /> YOUR LECTURES, UNDERSTOOD</span>
              <h1>Make every lecture<br />a <em>lasting advantage.</em></h1>
              <p>Turn recordings into thoughtful notes, then ask anything and jump straight to the moment it was taught.</p>
              <div className="hero-cta"><Link to="/sign-in" className="button button-dark button-large">Start learning free <ArrowRight size={17} /></Link><span>No credit card needed</span></div>
              <div className="hero-proof"><div className="avatar-stack"><i>J</i><i>A</i><i>M</i><i>+</i></div><span>Built for curious minds<br /><b>and busy semesters</b></span></div>
            </motion.div>
            <motion.div className="hero-art" initial={{ opacity: 0, scale: .94, rotate: 2 }} animate={{ opacity: 1, scale: 1, rotate: 0 }} transition={{ delay: .2, duration: .7 }}>
              <div className="art-glow" />
              <div className="lecture-card">
                <div className="lecture-card-top"><span className="tiny-file"><FileAudio2 size={16} /></span><span>LECTURE 08 · 42 MIN</span><span className="live-dot" /></div>
                <h3>Operating Systems</h3><p>Deadlocks &amp; resource allocation</p>
                <div className="waveform">{Array.from({ length: 44 }, (_, i) => <i key={i} style={{ height: `${12 + ((i * 37 + 13) % 50)}px` }} />)}</div>
                <div className="art-progress"><span /><small>18:42</small><small>42:08</small></div>
                <div className="art-note"><span className="note-spark"><Sparkles size={14} /></span><div><b>Key idea found</b><span>Four conditions for deadlock</span></div><span className="time-pill">18:42</span></div>
              </div>
              <div className="floating-tag"><Clock3 size={15} /> Answers with timestamps</div>
              <div className="floating-note"><BookOpen size={16} /><span>Notes, neatly organized</span><Check size={15} /></div>
            </motion.div>
          </section>
          <section className="trust-strip shell"><span>ONE PLACE FOR THE WHOLE LEARNING LOOP</span><div><i><AudioLines size={17} /> Listen</i><b>→</b><i><Sparkles size={17} /> Understand</i><b>→</b><i><MessageCircle size={17} /> Ask</i></div></section>
          <section className="feature-section shell" id="features">
            <span className="eyebrow">LESS REPLAYING. MORE LEARNING.</span><h2>Every detail, right where you need it.</h2>
            <div className="feature-grid">
              <article><span className="feature-icon mint"><FileText /></span><h3>Notes that make sense</h3><p>Key concepts, announcements and exam hints, organized from the whole lecture.</p></article>
              <article><span className="feature-icon peach"><Clock3 /></span><h3>Answers with a way back</h3><p>Ask a question and get a helpful answer linked to the exact moment in the audio.</p></article>
              <article><span className="feature-icon lilac"><FolderOpen /></span><h3>A calmer course library</h3><p>Subjects become folders. Your notes and chat history stay together and private.</p></article>
            </div>
          </section>
          <section className="bottom-cta shell" id="how"><div><span className="eyebrow">YOUR NEXT CLASS CAN START HERE</span><h2>Learn more from the time<br />you already spend listening.</h2></div><Link to="/sign-in" className="button button-dark button-large">Create your free space <ArrowRight size={17} /></Link></section>
          <footer className="site-footer shell"><Brand /><span>Your learning, in context.</span><span>© NotesGraph</span></footer>
        </motion.div>}
      </AnimatePresence>
    </main>
  );
}

function SignIn() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!supabase) { setError(supabaseConfigError); return; }
    setBusy(true); setError(""); setNotice("");
    try {
      const result = mode === "login"
        ? await supabase.auth.signInWithPassword({ email, password })
        : await supabase.auth.signUp({ email, password });
      if (result.error) {
        setError(result.error.message.toLowerCase().includes("invalid api key")
          ? "Supabase rejected the API key. Use this project's sb_publishable_ key in frontend/.env as VITE_SUPABASE_PUBLISHABLE_KEY, then restart Vite."
          : result.error.message);
        return;
      }
      if (mode === "signup" && !result.data.session) {
        setNotice("Check your inbox to confirm your email, then sign in.");
        return;
      }
      navigate("/app");
    } catch (reason) {
      setError((reason as Error).message || "Unable to reach Supabase Auth.");
    } finally {
      setBusy(false);
    }
  }
  return <main className="auth-page">
    <Link to="/" className="auth-back"><ArrowRight size={15} className="back-arrow" /> Back to home</Link>
    <div className="auth-art"><div className="auth-art-grid" /><div className="auth-art-copy">
      <Brand /><div><span className="eyebrow">A BETTER WAY TO REVISE</span><h1>Your classes,<br />made <em>clear.</em></h1><p>Everything you learned, organized and ready when you need it.</p></div>
      <div className="auth-art-foot"><span>Private by design</span><span>·</span><span>Made for learners</span></div>
    </div></div>
    <section className="auth-form-wrap">
      <div className="auth-form">
        <span className="eyebrow">{mode === "login" ? "WELCOME BACK" : "MAKE SPACE TO LEARN"}</span>
        <h2>{mode === "login" ? "Sign in to your space" : "Create your account"}</h2>
        <p>{mode === "login" ? "Your notes and conversations are right where you left them." : "A more useful way to keep up with every class."}</p>
        <div className="auth-tabs"><button className={mode === "login" ? "active" : ""} onClick={() => setMode("login")}>Sign in</button><button className={mode === "signup" ? "active" : ""} onClick={() => setMode("signup")}>Create account</button></div>
        <form onSubmit={submit}>
          <label>Email address<input type="email" autoComplete="email" required value={email} onChange={event => setEmail(event.target.value)} placeholder="you@school.edu" /></label>
          <label>Password<div className="password-input-wrap">
            <input type={showPassword ? "text" : "password"} autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={8} required value={password} onChange={event => setPassword(event.target.value)} placeholder="At least 8 characters" />
            <button className="password-visibility-toggle" type="button" aria-label={showPassword ? "Hide password" : "Show password"} aria-pressed={showPassword} onClick={() => setShowPassword(visible => !visible)}>
              {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
            </button>
          </div></label>
          {error && <div className="form-alert">{error}</div>}{notice && <div className="form-notice">{notice}</div>}
          <button className="button button-dark auth-submit" disabled={busy}>{busy ? <LoaderCircle className="spin" size={17} /> : null}{mode === "login" ? "Sign in" : "Create account"} <ArrowRight size={16} /></button>
        </form>
        <small className="privacy-note"><span>🔒</span> Your recordings and notes are private to your account.</small>
      </div>
    </section>
  </main>;
}

function LoadingScreen() {
  return <div className="loading-screen"><span className="brand-mark"><AudioLines size={19} /></span><LoaderCircle className="spin" size={21} /></div>;
}

function Dashboard({ session, theme }: { session: Session; theme: ReturnType<typeof useTheme> }) {
  const navigate = useNavigate();
  const [lectures, setLectures] = useState<Lecture[]>([]);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [selected, setSelected] = useState<Lecture | null>(null);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [chatSession, setChatSession] = useState<ChatSession | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const chatLoadRequest = useRef(0);
  const [view, setView] = useState<"notes" | "chat" | "transcript">("notes");
  const [transcript, setTranscript] = useState("");
  const [folderFilter, setFolderFilter] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [showUpload, setShowUpload] = useState(false);
  const [mobileMenu, setMobileMenu] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const userName = session.user.email?.split("@")[0] || "Learner";

  const refresh = useCallback(async () => {
    const [lectureRows, folderRows] = await Promise.all([
      api<Lecture[]>("/lectures"), api<Folder[]>("/folders"),
    ]);
    setLectures(lectureRows); setFolders(folderRows);
    return lectureRows;
  }, []);
  useEffect(() => { refresh().catch(reason => setError(reason.message)); }, [refresh]);

  const openLecture = useCallback(async (lectureId: string) => {
    chatLoadRequest.current += 1;
    setLoading(true); setError(""); setView("notes"); setChatSession(null); setMessages([]);
    try {
      const data = await api<Lecture>(`/lectures/${lectureId}`);
      setSelected(data);
      const rows = await api<ChatSession[]>(`/chat-sessions?lecture_id=${lectureId}`);
      setSessions(rows);
    } catch (reason) { setError((reason as Error).message); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    if (!selected || !["queued", "transcribing", "generating_notes", "indexing"].includes(selected.status)) return;
    let cancelled = false;
    let timer: number | undefined;
    let retryDelay = 2500;
    const poll = async () => {
      try {
        const data = await api<Lecture>(`/lectures/${selected.id}`);
        retryDelay = 2500;
        setSelected(data); setLectures(current => current.map(item => item.id === data.id ? data : item));
        setError("");
        if (data.status === "done" || data.status === "error" || data.status === "index_error") {
          await refresh();
          return;
        }
      } catch (reason) {
        setError((reason as Error).message);
        retryDelay = Math.min(retryDelay * 2, 20_000);
      }
      if (!cancelled) timer = window.setTimeout(poll, retryDelay);
    };
    timer = window.setTimeout(poll, retryDelay);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [selected, refresh]);

  const filteredLectures = useMemo(() => lectures.filter(item =>
    (!folderFilter || item.folder_id === folderFilter) &&
    `${item.title} ${item.subject}`.toLowerCase().includes(query.toLowerCase()),
  ), [lectures, folderFilter, query]);

  async function chooseChat(chat: ChatSession, loadMessages = true) {
    setChatSession(chat); setView("chat"); setError("");
    setSessions(current => current.some(item => item.id === chat.id)
      ? current.map(item => item.id === chat.id ? chat : item)
      : [chat, ...current]);
    if (!loadMessages) {
      chatLoadRequest.current += 1;
      return;
    }
    const requestId = ++chatLoadRequest.current;
    setMessages([]);
    try {
      const rows = await api<ChatMessage[]>(`/chat-sessions/${chat.id}/messages`);
      if (requestId === chatLoadRequest.current) setMessages(rows);
    } catch (reason) {
      if (requestId === chatLoadRequest.current) setError((reason as Error).message);
    }
  }

  async function createChat() {
    if (!selected) return;
    try {
      const chat = await api<ChatSession>(`/lectures/${selected.id}/chat-sessions`, { method: "POST" });
      setSessions(current => [chat, ...current]); setChatSession(chat); setMessages([]); setView("chat");
    } catch (reason) { setError((reason as Error).message); }
  }

  async function deleteLecture(id: string) {
    if (!window.confirm("Delete this lecture, its notes, and its chat history?")) return;
    try { await api(`/lectures/${id}`, { method: "DELETE" }); setSelected(null); await refresh(); }
    catch (reason) { setError((reason as Error).message); }
  }

  async function retryLecture(id: string) {
    try {
      setSelected(await api<Lecture>(`/lectures/${id}/retry`, { method: "POST" }));
      await refresh();
    } catch (reason) { setError((reason as Error).message); }
  }

  async function retryIndex(id: string) {
    try {
      setSelected(await api<Lecture>(`/lectures/${id}/retry-index`, { method: "POST" }));
      await refresh();
    } catch (reason) { setError((reason as Error).message); }
  }

  async function showTranscript() {
    if (!selected) return;
    setView("transcript"); setTranscript("");
    try {
      if (!supabase) throw new Error(supabaseConfigError);
      const { data: { session: active } } = await supabase.auth.getSession();
      const response = await fetch(apiUrl(`/lectures/${selected.id}/transcript`), { headers: { Authorization: `Bearer ${active?.access_token}` } });
      if (!response.ok) throw new Error("Transcript is not ready yet.");
      setTranscript(await response.text());
    } catch (reason) { setError((reason as Error).message); }
  }

  async function download(path: string, filename: string) {
    try {
      if (!supabase) throw new Error(supabaseConfigError);
      const { data: { session: active } } = await supabase.auth.getSession();
      const response = await fetch(apiUrl(path), { headers: { Authorization: `Bearer ${active?.access_token}` } });
      if (!response.ok) throw new Error("Unable to download this file.");
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = filename; anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) { setError((reason as Error).message); }
  }

  async function signOut() {
    await supabase?.auth.signOut(); navigate("/");
  }

  return <div className="workspace">
    <aside className={`sidebar ${mobileMenu ? "sidebar-open" : ""}`}>
      <div className="sidebar-head"><Brand /><button className="icon-btn mobile-close" onClick={() => setMobileMenu(false)}><X size={18} /></button></div>
      <button className="new-lecture" onClick={() => setShowUpload(true)}><Plus size={18} /> New lecture <span>⌘ K</span></button>
      <nav className="side-nav">
        <button className={!folderFilter ? "side-link selected" : "side-link"} onClick={() => { setFolderFilter(null); setSelected(null); }}><LayoutDashboard size={17} /> All lectures <span>{lectures.length}</span></button>
        <div className="side-label">YOUR SUBJECTS <button title="Create folder when adding a subject"><Plus size={14} /></button></div>
        {folders.map(folder => <button key={folder.id} className={folderFilter === folder.id ? "side-link selected" : "side-link"} onClick={() => { setFolderFilter(folder.id); setSelected(null); }}><FolderClosed size={16} />{folder.name}<span>{lectures.filter(item => item.folder_id === folder.id).length}</span></button>)}
      </nav>
      <div className="sidebar-bottom">
        <div className="private-card"><span className="private-icon"><Sparkles size={16} /></span><div><b>Your private library</b><small>Only you can see your content.</small></div></div>
        <div className="profile-row"><span className="profile-avatar">{userName.slice(0, 1).toUpperCase()}</span><div className="profile-name"><b>{userName}</b><small>Personal workspace</small></div><button className="icon-btn" title="Sign out" onClick={signOut}><LogOut size={17} /></button></div>
      </div>
    </aside>
    {mobileMenu && <button className="mobile-backdrop" onClick={() => setMobileMenu(false)} aria-label="Close menu" />}
    <main className="work-main">
      <header className="work-topbar"><button className="icon-btn mobile-menu" onClick={() => setMobileMenu(true)}><Menu size={19} /></button>
        <div className="breadcrumbs"><span>My workspace</span><span>/</span><b>{selected?.title || folders.find(folder => folder.id === folderFilter)?.name || "All lectures"}</b></div>
        <div className="work-actions"><span className="secure-tag"><span /> Private workspace</span><ThemeButton {...theme} /><span className="profile-avatar top-avatar">{userName.slice(0, 1).toUpperCase()}</span></div>
      </header>
      <div className="work-content">
        {error && <div className="error-banner"><span>{error}</span><button onClick={() => setError("")}><X size={15} /></button></div>}
        {!selected ? <section className="library-view">
          <div className="welcome-row"><div><span className="eyebrow">YOUR LEARNING SPACE</span><h1>{folderFilter ? folders.find(folder => folder.id === folderFilter)?.name : `Good to see you, ${userName}`}</h1><p>{folderFilter ? "Your course notes and lecture recordings in one place." : "Pick up where you left off, or make something new."}</p></div>
            <button className="button button-dark" onClick={() => setShowUpload(true)}><Plus size={17} /> Add a lecture</button></div>
          <div className="library-toolbar"><div className="searchbox"><Search size={16} /><input placeholder="Search lectures..." value={query} onChange={event => setQuery(event.target.value)} /></div><button className="sort-button">Recently added <ChevronDown size={15} /></button></div>
          {loading ? <div className="empty-state"><LoaderCircle className="spin" /></div> : filteredLectures.length ? <div className="lecture-grid">
            {filteredLectures.map((lecture, index) => <motion.button key={lecture.id} className="library-card" onClick={() => openLecture(lecture.id)} initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: Math.min(index * .04, .25) }}>
              <span className={`course-cover cover-${index % 4}`}><AudioLines size={24} /><span>{lecture.subject || "LECTURE"}</span></span>
              <span className="library-card-info"><b>{lecture.title}</b><small><span>{lecture.subject || "Uncategorized"}</span><span>·</span><span>{new Date(lecture.created_at).toLocaleDateString()}</span></small>
                <span className={`status-chip ${lecture.status === "done" || lecture.status === "index_error" ? "ready" : lecture.status === "error" ? "failed" : "processing"}`}><i />{lecture.status === "done" ? "Notes ready" : lecture.status === "index_error" ? "Notes ready · chat needs retry" : lecture.status === "error" ? "Needs attention" : lecture.progress || "Processing"}</span>
              </span><ArrowRight className="card-arrow" size={17} /></motion.button>)}
          </div> : <div className="empty-library"><div className="empty-art"><BookOpen size={27} /><Sparkles size={16} /></div><h3>{query ? "No lectures found" : "Your first lecture starts here"}</h3><p>{query ? "Try another search." : "Add a recording or paste a transcript to create your first set of notes."}</p>{!query && <button className="button button-dark" onClick={() => setShowUpload(true)}><Plus size={17} /> Add your first lecture</button>}</div>}
          <div className="library-footnote"><span><Check size={14} /> Private to your account</span><span>{lectures.length} {lectures.length === 1 ? "lecture" : "lectures"} saved</span></div>
        </section> : <LectureView lecture={selected} view={view} setView={setView} onBack={() => setSelected(null)} onDelete={() => deleteLecture(selected.id)} onRetry={() => retryLecture(selected.id)} onRetryIndex={() => retryIndex(selected.id)} onDownload={download}
          sessions={sessions} chatSession={chatSession} messages={messages} onCreateChat={createChat} onChooseChat={chooseChat} setMessages={setMessages} transcript={transcript} onTranscript={showTranscript} />}
      </div>
    </main>
    <AnimatePresence>{showUpload && <UploadModal onClose={() => setShowUpload(false)} onCreated={lecture => {
      setSelected(lecture);
      setView("notes");
      setError("");
      void refresh().catch(reason => setError((reason as Error).message));
    }} />}</AnimatePresence>
  </div>;
}

function LectureView({ lecture, view, setView, onBack, onDelete, onRetry, onRetryIndex, onDownload, sessions, chatSession: active, messages, onCreateChat: onCreate, onChooseChat: onChoose, setMessages, transcript, onTranscript }: {
  lecture: Lecture; view: "notes" | "chat" | "transcript"; setView: (view: "notes" | "chat" | "transcript") => void;
  onBack: () => void; onRetry: () => void; onRetryIndex: () => void;
  onDelete: () => void; onDownload: (path: string, filename: string) => void; sessions: ChatSession[];
  chatSession: ChatSession | null; messages: ChatMessage[]; onCreateChat: () => void;
  onChooseChat: (session: ChatSession, loadMessages?: boolean) => void;
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
  transcript: string; onTranscript: () => void;
}) {
  return <motion.section className="lecture-detail" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
    <button className="back-library" onClick={onBack}><ArrowRight className="back-arrow" size={15} /> Library</button>
    <div className="detail-heading"><div><span className="eyebrow">{lecture.subject || "LECTURE NOTES"}</span><h1>{lecture.title}</h1><p>{lecture.source} <span>·</span> Added {new Date(lecture.created_at).toLocaleDateString()}</p></div>
      <div className="detail-actions">{(lecture.status === "done" || lecture.status === "index_error") && <><button className="button button-outline" onClick={() => onDownload(`/lectures/${lecture.id}/notes.pdf`, `${lecture.title}.pdf`)}><ArrowDownToLine size={16} /> PDF</button><button className="button button-outline" onClick={() => onDownload(`/lectures/${lecture.id}/notes.md`, `${lecture.title}.md`)}><ArrowDownToLine size={16} /> Markdown</button></>}
        {lecture.status === "error" && <button className="button button-outline" onClick={onRetry}>Retry processing</button>}
        {lecture.status === "index_error" && <button className="button button-outline" onClick={onRetryIndex}>Retry chat indexing</button>}
        {(lecture.status === "done" || lecture.status === "error" || lecture.status === "index_error") && <button className="icon-btn danger-icon" title="Delete lecture" onClick={onDelete}><Trash2 size={17} /></button>}</div>
    </div>
    {lecture.status !== "done" && lecture.status !== "index_error" && <div className={`process-banner ${lecture.status === "error" ? "process-error" : ""}`}>
      {lecture.status === "error" ? <span>{lecture.error || "Something went wrong while processing."}</span> : <><LoaderCircle className="spin" size={18} /><div><b>{lecture.progress || "Getting your notes ready"}</b><small>Leave this page open or come back soon.</small></div></>}
    </div>}
    {lecture.status === "index_error" && <div className="process-banner process-error">{lecture.error || "Your notes are ready. Chat indexing can be retried without regenerating them."}</div>}
    {(lecture.status === "done" || lecture.status === "index_error") && <div className="detail-layout">
      <div className="detail-primary">
        <div className="detail-tabs"><button className={view === "notes" ? "active" : ""} onClick={() => setView("notes")}><FileText size={16} /> Study notes</button><button className={view === "chat" ? "active" : ""} onClick={() => setView("chat")}><MessageCircle size={16} /> Ask this lecture</button><button className={view === "transcript" ? "active" : ""} onClick={onTranscript}><AudioLines size={16} /> Transcript</button></div>
        {view === "notes" && <NotesView notes={lecture.notes} />}
        {view === "chat" && <ChatView lectureId={lecture.id} sessions={sessions} active={active} messages={messages} onCreate={onCreate} onChoose={onChoose} setMessages={setMessages} />}
        {view === "transcript" && <div className="transcript-panel"><div className="panel-title"><div><span className="eyebrow">FROM THE RECORDING</span><h2>Lecture transcript</h2></div><span className="transcript-readonly"><Check size={14} /> Source transcript</span></div><pre>{transcript || "Loading transcript…"}</pre></div>}
      </div>
      <aside className="detail-aside"><div className="aside-card"><span className="aside-icon"><Sparkles size={17} /></span><span className="eyebrow">QUICK OVERVIEW</span><p>{lecture.notes?.summary || "Your lecture summary will appear here."}</p><div className="aside-stats"><span><b>{lecture.notes?.core_explanations?.length || 0}</b> topics</span><span><b>{lecture.notes?.exam_points?.length || 0}</b> exam tips</span></div></div>
        <div className="aside-card chat-history-card"><div className="history-heading"><span className="eyebrow">CHAT HISTORY</span><button title="Start a new chat" onClick={onCreate}><Plus size={16} /></button></div>
          {sessions.length ? sessions.map(chat => <button key={chat.id} className={`history-item ${active?.id === chat.id ? "current" : ""}`} onClick={() => onChoose(chat)}><MessageCircle size={15} /><span>{chat.title}</span><ArrowRight size={13} /></button>) : <p className="history-empty">Your questions about this lecture will stay here.</p>}
        </div></aside>
    </div>}
  </motion.section>;
}

function NotesView({ notes }: { notes?: Notes }) {
  if (!notes) return <div className="empty-library"><h3>Notes are being prepared</h3><p>They will appear here when the lecture is ready.</p></div>;
  return <article className="notes-page"><div className="notes-intro"><span className="eyebrow">LECTURE SUMMARY</span><p>{notes.summary}</p></div>
    <NoteSection title="Core explanations" tone="blue" icon={<BookOpen size={17} />}>{notes.core_explanations.length ? notes.core_explanations.map((topic, index) => <div className="concept" key={`${topic.topic}-${index}`}><h3>{topic.topic}</h3><p>{topic.explanation}</p>{topic.key_points?.length > 0 && <ul>{topic.key_points.map(point => <li key={point}>{point}</li>)}</ul>}</div>) : <p className="empty-note">No academic content was identified.</p>}</NoteSection>
    <NoteSection title="Exam-relevant points" tone="rose" icon={<Sparkles size={17} />}>{notes.exam_points.length ? notes.exam_points.map((item, index) => <div className="exam-note" key={index}><span>{item.kind.replaceAll("_", " ")}</span>{item.point}</div>) : <p className="empty-note">No exam hints were identified.</p>}</NoteSection>
    <NoteSection title="Announcements" tone="amber" icon={<Clock3 size={17} />}>{notes.announcements.length ? notes.announcements.map((item, index) => <div className="announcement" key={index}><span>{item.text}</span>{item.deadline && <b>{item.deadline}</b>}</div>) : <p className="empty-note">No announcements were identified.</p>}</NoteSection>
    <NoteSection title="Off-topic remarks" tone="green" icon={<MessageCircle size={17} />}>{notes.off_topic.length ? notes.off_topic.map((item, index) => <p className="off-topic" key={index}>{item.summary}</p>) : <p className="empty-note">The class stayed on topic.</p>}</NoteSection>
  </article>;
}

function NoteSection({ title, tone, icon, children }: { title: string; tone: string; icon: ReactNode; children: ReactNode }) {
  return <section className={`note-section ${tone}`}><div className="note-section-title"><span>{icon}</span><h2>{title}</h2></div>{children}</section>;
}

function ChatView({ lectureId, sessions, active, messages, onCreate, onChoose, setMessages }: {
  lectureId: string; sessions: ChatSession[]; active: ChatSession | null; messages: ChatMessage[];
  onCreate: () => void; onChoose: (session: ChatSession, loadMessages?: boolean) => void;
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
}) {
  const [question, setQuestion] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const hasPendingReply = messages.some(
    message => message.role === "assistant" && message.status === "pending",
  );
  useEffect(() => {
    if (!active || !hasPendingReply) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const rows = await api<ChatMessage[]>(`/chat-sessions/${active.id}/messages`);
        if (cancelled) return;
        setMessages(rows);
        if (rows.some(message => message.role === "assistant" && message.status === "pending")) {
          timer = window.setTimeout(poll, 1800);
        }
      } catch (reason) {
        if (cancelled) return;
        setError((reason as Error).message);
        timer = window.setTimeout(poll, 4000);
      }
    };
    timer = window.setTimeout(poll, 1200);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [active, hasPendingReply, setMessages]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const text = question.trim();
    if (!text || sending) return;
    let chat = active;
    const temporaryId = -Date.now();
    const optimistic: ChatMessage = {
      id: temporaryId,
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
    };
    const optimisticReply: ChatMessage = {
      id: temporaryId - 1,
      role: "assistant",
      content: "",
      created_at: new Date().toISOString(),
      status: "pending",
      sources: [],
    };
    setSending(true);
    setError("");
    setMessages(current => [...current, optimistic, optimisticReply]);
    try {
      if (!chat) {
        chat = await api<ChatSession>(`/lectures/${lectureId}/chat-sessions`, { method: "POST" });
        onChoose(chat, false);
      }
      const turn = await api<{ user_message: ChatMessage; assistant_message: ChatMessage }>(`/lectures/${lectureId}/chat`, {
        method: "POST", body: JSON.stringify({ session_id: chat.id, message: text }),
      });
      setMessages(current => [
        ...current.filter(message => message.id !== optimistic.id && message.id !== optimisticReply.id),
        turn.user_message,
        turn.assistant_message,
      ]);
      setQuestion("");
      onChoose({ ...chat, title: text.slice(0, 80) }, false);
    } catch (reason) {
      setMessages(current => current.map(message => message.id === optimisticReply.id
        ? { ...message, status: "error", content: "The question could not be sent. Please try again." }
        : message));
      setError((reason as Error).message);
    }
    finally { setSending(false); }
  }
  return <div className="chat-panel">
    <div className="chat-panel-head"><div><span className="eyebrow">LECTURE ASSISTANT</span><h2>{active?.title || "Ask anything from class"}</h2></div>
      <div className="chat-head-actions"><button className="button button-outline compact" onClick={() => setShowHistory(!showHistory)}><Clock3 size={15} /> History <ChevronDown size={14} /></button><button className="button button-outline compact" onClick={onCreate}><Plus size={15} /> New chat</button></div>
    </div>
    {showHistory && <div className="chat-history-dropdown">{sessions.map(chat => <button key={chat.id} onClick={() => { onChoose(chat); setShowHistory(false); }}><MessageCircle size={15} />{chat.title}</button>)}</div>}
    <div className="chat-messages">
      {!messages.length && <div className="chat-welcome"><span><Sparkles size={20} /></span><h3>Your lecture, on demand.</h3><p>Ask a question and I’ll point you to the relevant moment in the recording.</p><div className="prompt-suggestions">{["What should I focus on for the exam?", "Explain the main idea simply", "Where does the lecture discuss its key example?"].map(text => <button key={text} onClick={() => setQuestion(text)}>{text} <ArrowRight size={14} /></button>)}</div></div>}
      {messages.map(message => <div key={message.id} className={`chat-message ${message.role}`}><span className="message-avatar">{message.role === "assistant" ? <Sparkles size={15} /> : "Y"}</span><div className={`message-content ${message.status === "error" ? "message-failed" : ""}`}>{message.status === "pending" ? <div className="thinking"><span /><span /><span /></div> : <p>{message.content}</p>}{message.sources?.some(source => source.timestamp) && <div className="source-list"><span>IN THE RECORDING</span>{message.sources.filter(source => source.timestamp).map((source, index) => <span className="source-chip" key={`${source.timestamp}-${index}`} title={source.text}><Clock3 size={13} /> {source.timestamp}</span>)}</div>}</div></div>)}
    </div>
    {error && <div className="chat-error">{error}</div>}
    <form className="chat-composer" onSubmit={submit}><textarea value={question} onChange={event => setQuestion(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder="Ask a question about this lecture…" rows={1} />
      <span>{hasPendingReply ? "Waiting for the previous answer…" : "Answers are grounded in your lecture"}</span><button disabled={sending || hasPendingReply || !question.trim()} aria-label="Send question">{sending ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />}</button>
    </form>
  </div>;
}

function UploadModal({ onClose, onCreated }: { onClose: () => void; onCreated: (lecture: Lecture) => void }) {
  const [mode, setMode] = useState<"audio" | "transcript">("audio");
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [subject, setSubject] = useState("");
  const [transcript, setTranscript] = useState("");
  const [language, setLanguage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [drag, setDrag] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (mode === "audio" && !file) { setError("Choose an audio file to continue."); return; }
    if (mode === "transcript" && !transcript.trim()) { setError("Paste a transcript to continue."); return; }
    const form = new FormData(); form.append("title", title); form.append("subject", subject);
    if (mode === "audio" && file) { form.append("file", file); form.append("language", language); }
    else form.append("transcript_text", transcript);
    setBusy(true); setError("");
    try {
      const lecture = await api<Lecture>("/lectures", { method: "POST", body: form });
      onClose();
      onCreated(lecture);
    }
    catch (reason) { setError((reason as Error).message); setBusy(false); }
  }
  return <motion.div className="modal-backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <motion.section className="upload-modal" initial={{ opacity: 0, y: 18, scale: .98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 12 }} transition={{ duration: .2 }}>
      <div className="modal-head"><div><span className="eyebrow">A NEW CLASS, MADE CLEAR</span><h2>Add a lecture</h2><p>Upload an audio recording or bring in a transcript.</p></div><button className="icon-btn" onClick={onClose}><X size={19} /></button></div>
      <div className="upload-tabs"><button type="button" className={mode === "audio" ? "active" : ""} onClick={() => setMode("audio")}><FileAudio2 size={16} /> Audio recording</button><button type="button" className={mode === "transcript" ? "active" : ""} onClick={() => setMode("transcript")}><FileText size={16} /> Paste transcript</button></div>
      <form onSubmit={submit}>
        {mode === "audio" ? <label className={`drop-zone ${drag ? "dragging" : ""}`} onDragOver={event => { event.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={event => { event.preventDefault(); setDrag(false); setFile(event.dataTransfer.files[0] || null); }}>
          <input type="file" accept=".mp3,.wav,.m4a,.mp4,.aac,.ogg,.flac,.webm,.opus,.wma,audio/*" onChange={event => setFile(event.target.files?.[0] || null)} />
          <span className="upload-icon"><UploadCloud size={21} /></span><b>{file ? file.name : "Drop your audio here"}</b><span>{file ? `${(file.size / (1024 * 1024)).toFixed(1)} MB · Ready to upload` : "or click to browse · MP3, WAV, M4A and more"}</span>
        </label> : <label className="transcript-field"><span>Paste your lecture transcript</span><textarea rows={7} value={transcript} onChange={event => setTranscript(event.target.value)} placeholder="Paste your transcript here…" /></label>}
        <div className="upload-fields"><label>Title <small>Optional</small><input value={title} onChange={event => setTitle(event.target.value)} placeholder="We’ll create one from your lecture" /></label>
          <label>Subject folder <small>Optional</small><input value={subject} onChange={event => setSubject(event.target.value)} placeholder="e.g. Operating Systems" /></label>
        </div>
        {mode === "audio" && <label className="language-select">Audio language<select value={language} onChange={event => setLanguage(event.target.value)}><option value="">Auto-detect</option><option value="en">English</option><option value="hi">Hindi</option><option value="gu">Gujarati</option></select></label>}
        {error && <div className="form-alert">{error}</div>}
        <div className="modal-foot"><span><span className="secure-dot" /> Private to your account</span><button className="button button-dark" disabled={busy}>{busy ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={16} />}{busy ? mode === "audio" ? "Uploading…" : "Starting…" : "Create my notes"} <ArrowRight size={16} /></button></div>
      </form>
    </motion.section>
  </motion.div>;
}

export default App;
