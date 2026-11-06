import {
  type Dispatch,
  type FormEvent,
  type ReactNode,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ArrowDownToLine,
  ArrowRight,
  AudioLines,
  BookOpen,
  Check,
  ChevronDown,
  Clock3,
  Eye,
  EyeOff,
  FileAudio2,
  FileText,
  FolderClosed,
  FolderOpen,
  LayoutDashboard,
  LoaderCircle,
  LogOut,
  Menu,
  MessageCircle,
  Moon,
  Plus,
  Search,
  Send,
  Sparkles,
  Sun,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import ReactMarkdown from "react-markdown";
import {
  Link,
  Navigate,
  Route,
  Routes,
  useMatch,
  useNavigate,
} from "react-router-dom";
import { Upload } from "tus-js-client";
import type { Session } from "@supabase/supabase-js";
import {
  supabase,
  supabaseConfigError,
  supabasePublishableKey,
  supabaseUrl,
} from "./supabase";

type Lecture = {
  id: string;
  title: string;
  subject: string;
  folder_id: string | null;
  source: string;
  status: string;
  progress: string;
  error: string;
  created_at: string;
  notes?: Notes;
  audio_path?: string | null;
};
type Notes = {
  summary: string;
  core_explanations: {
    topic: string;
    explanation: string;
    key_points: string[];
  }[];
  announcements: { text: string; deadline: string }[];
  exam_points: { point: string; kind: string }[];
  off_topic: { summary: string }[];
};
type Folder = { id: string; name: string };
type ChatSession = {
  id: string;
  lecture_id: string;
  title: string;
  updated_at: string;
};
type ChatSource = {
  start?: number;
  end?: number;
  timestamp_label?: string;
  text_snippet?: string;
};
type ChatMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  status?: "pending" | "complete" | "error";
  sources?: ChatSource[];
  intent?: "locate" | "explain" | "off_topic";
  grounded?: boolean;
};

function formatSourceTime(seconds: number) {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return hours
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function sourceLabel(source: ChatSource) {
  if (typeof source.start === "number") {
    const start = formatSourceTime(source.start);
    return typeof source.end === "number" && source.end > source.start
      ? `${start}–${formatSourceTime(source.end)}`
      : start;
  }
  return source.timestamp_label || "";
}

function displaySources(message: ChatMessage) {
  if (message.grounded === false || !message.sources?.length) return [];
  const unique = new Map(
    message.sources
      .filter((source) => typeof source.start === "number")
      .map((source) => [
        `${source.start}-${source.end ?? source.start}`,
        source,
      ]),
  );
  return [...unique.values()].sort(
    (left, right) => (left.start ?? 0) - (right.start ?? 0),
  );
}
const configuredApiUrl = (
  window.NOTESGRAPH_CONFIG?.apiUrl ||
  import.meta.env.VITE_API_URL ||
  ""
).trim();
const apiBaseError = (() => {
  if (!configuredApiUrl) return "";
  try {
    const url = new URL(configuredApiUrl);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      (url.pathname !== "/" && url.pathname !== "") ||
      url.search ||
      url.hash
    ) {
      return "VITE_API_URL must be the backend origin only, such as http://localhost:8000. Do not put an API token or path there.";
    }
    return "";
  } catch {
    return "VITE_API_URL must be a backend URL such as http://localhost:8000. Leave it empty when using the local Vite proxy.";
  }
})();
const apiBase =
  configuredApiUrl && !apiBaseError ? new URL(configuredApiUrl).origin : "";

function apiUrl(path: string) {
  if (apiBaseError) throw new Error(apiBaseError);
  return `${apiBase}/api${path}`;
}

function readCache<T>(key: string): T | null {
  try {
    const value = localStorage.getItem(key);
    return value ? (JSON.parse(value) as T) : null;
  } catch (reason) {
    console.warn(`Could not read dashboard cache '${key}'.`, reason);
    return null;
  }
}

function writeCache(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (reason) {
    console.warn(`Could not update dashboard cache '${key}'.`, reason);
  }
}

function audioUploadKey(userId: string, file: File) {
  return [
    "notesgraph-audio-upload",
    encodeURIComponent(userId),
    encodeURIComponent(file.name),
    file.size,
    file.lastModified,
  ].join(":");
}

function getAudioObjectPath(userId: string, file: File) {
  const key = audioUploadKey(userId, file);
  const existingPath = localStorage.getItem(key);
  if (existingPath?.startsWith(`${userId}/`))
    return { key, path: existingPath };

  const extension = file.name.match(/\.[^.]+$/)?.[0].toLowerCase() || "";
  const path = `${userId}/${crypto.randomUUID()}${extension}`;
  localStorage.setItem(key, path);
  return { key, path };
}

const TUS_CHUNK_SIZE = 6 * 1024 * 1024;

async function uploadAudioDirect(
  file: File,
  objectPath: string,
  storageOrigin: string,
  accessToken: string,
): Promise<void> {
  const objectUrl = new URL(
    `/storage/v1/object/lecture-audio/${objectPath.split("/").map(encodeURIComponent).join("/")}`,
    storageOrigin,
  );
  const response = await fetch(objectUrl, {
    method: "POST",
    headers: {
      authorization: `Bearer ${accessToken}`,
      apikey: supabasePublishableKey,
      "cache-control": "max-age=3600",
      "content-type": file.type || "application/octet-stream",
      "x-upsert": "true",
    },
    body: file,
  });
  if (!response.ok) {
    if (response.status === 524) {
      throw new Error(
        "Supabase Storage timed out while receiving this file (Cloudflare 524). The upload was not confirmed. Retry the same file to resume, and check Supabase service status if this keeps happening.",
      );
    }
    const body = await response.text();
    throw new Error(
      `Direct Storage upload failed (${response.status}): ${body.slice(0, 500) || response.statusText}`,
    );
  }
}

function uploadAudioToStorage(
  file: File,
  objectPath: string,
  accessToken: string,
  fallbackKey: string,
  onProgress: (percentage: number | null) => void,
): Promise<void> {
  if (!supabaseUrl) throw new Error(supabaseConfigError);
  const storageUrl = new URL(supabaseUrl);
  if (
    storageUrl.hostname.endsWith(".supabase.co") &&
    !storageUrl.hostname.endsWith(".storage.supabase.co")
  ) {
    storageUrl.hostname = `${storageUrl.hostname.slice(0, -".supabase.co".length)}.storage.supabase.co`;
  }
  const canUseDirectFallback = file.size < TUS_CHUNK_SIZE;
  if (!canUseDirectFallback) localStorage.removeItem(fallbackKey);
  if (canUseDirectFallback && localStorage.getItem(fallbackKey) === "true") {
    onProgress(null);
    return uploadAudioDirect(
      file,
      objectPath,
      storageUrl.origin,
      accessToken,
    ).catch((reason) => {
      localStorage.removeItem(fallbackKey);
      throw reason;
    });
  }
  return new Promise((resolve, reject) => {
    let networkFailures = 0;
    const upload = new Upload(file, {
      endpoint: `${storageUrl.origin}/storage/v1/upload/resumable`,
      metadata: {
        bucketName: "lecture-audio",
        objectName: objectPath,
        contentType: file.type || "application/octet-stream",
        cacheControl: "3600",
      },
      chunkSize: TUS_CHUNK_SIZE,
      retryDelays: [0, 3000, 5000, 10000, 20000, 30000, 60000, 120000],
      uploadDataDuringCreation: true,
      headers: {
        authorization: `Bearer ${accessToken}`,
        apikey: supabasePublishableKey,
        "x-upsert": "true",
      },
      fingerprint: async () =>
        `notesgraph:${objectPath}:${file.size}:${file.lastModified}`,
      removeFingerprintOnSuccess: true,
      storeFingerprintForResuming: true,
      onShouldRetry: (error) => {
        const status = error.originalResponse?.getStatus() || 0;
        if (status === 0) {
          networkFailures += 1;
          return navigator.onLine !== false && networkFailures < 2;
        }
        return (
          status === 409 || status === 423 || status < 400 || status >= 500
        );
      },
      onProgress: (bytesSent, bytesTotal) => {
        onProgress(Math.round((bytesSent / bytesTotal) * 100));
      },
      onError: (reason) => {
        const status =
          "originalResponse" in reason
            ? reason.originalResponse?.getStatus() || 0
            : 0;
        if (status === 524) {
          reject(
            new Error(
              "Supabase Storage timed out while receiving an upload chunk (Cloudflare 524). Retry the same file to resume; if this keeps happening, check Supabase service status or contact Supabase support with the request time and Cloudflare Ray ID.",
            ),
          );
          return;
        }
        if (
          "originalRequest" in reason &&
          status === 0 &&
          canUseDirectFallback
        ) {
          localStorage.setItem(fallbackKey, "true");
          onProgress(null);
          void uploadAudioDirect(
            file,
            objectPath,
            storageUrl.origin,
            accessToken,
          )
            .then(resolve)
            .catch((fallbackError) => {
              localStorage.removeItem(fallbackKey);
              reject(
                new Error(
                  `Resumable upload lost its network connection. The small-file fallback also failed: ${userFacingError(fallbackError)}`,
                ),
              );
            });
          return;
        }
        if ("originalRequest" in reason && status === 0) {
          reject(
            new Error(
              "The connection to Supabase Storage was lost during a chunk upload. The resumable upload is preserved; retry the same file to continue from its last saved chunk. If it keeps failing, check Supabase service status and your network.",
            ),
          );
          return;
        }
        reject(reason);
      },
      onSuccess: () => resolve(),
    });
    void upload
      .findPreviousUploads()
      .then((previousUploads) => {
        if (previousUploads.length)
          upload.resumeFromPreviousUpload(previousUploads[0]);
        upload.start();
      })
      .catch(reject);
  });
}

function isAbortError(reason: unknown): boolean {
  const error = reason as {
    name?: unknown;
    message?: unknown;
    cause?: unknown;
  } | null;
  const name = typeof error?.name === "string" ? error.name : "";
  const message =
    typeof error?.message === "string"
      ? error.message
      : typeof reason === "string"
        ? reason
        : "";
  const normalized = `${name} ${message}`.toLowerCase();
  return (
    name === "AbortError" ||
    name === "TimeoutError" ||
    normalized.includes("signal is aborted") ||
    normalized.includes("aborted without any reason") ||
    normalized.includes("the operation was aborted") ||
    (!!error?.cause && error.cause !== reason && isAbortError(error.cause))
  );
}

function userFacingError(
  reason: unknown,
  fallback = "The request failed unexpectedly. Please try again.",
): string {
  const error = reason as {
    name?: unknown;
    message?: unknown;
    cause?: unknown;
  } | null;
  if (isAbortError(reason)) {
    return "The connection was interrupted before the request finished. Please retry. If it keeps happening, refresh the page and check the Render service logs.";
  }
  const message =
    typeof error?.message === "string"
      ? error.message.trim()
      : typeof reason === "string"
        ? reason.trim()
        : "";
  if (message) return message;
  if (error?.cause && error.cause !== reason)
    return userFacingError(error.cause, fallback);
  return fallback;
}

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!supabase) throw new Error(supabaseConfigError);
  if (apiBaseError) throw new Error(apiBaseError);
  let session;
  try {
    const { data, error } = await supabase.auth.getSession();
    if (error) throw error;
    session = data.session;
  } catch (reason) {
    throw new Error(
      userFacingError(
        reason,
        "Unable to verify your sign-in session. Please sign in again.",
      ),
    );
  }
  if (!session?.access_token)
    throw new Error("Your session expired. Please sign in again.");
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${session.access_token}`);
  if (
    !(init.body instanceof FormData) &&
    init.body &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      headers,
      signal:
        init.signal ||
        AbortSignal.timeout(
          init.body instanceof FormData || path.endsWith("/chat")
            ? 180_000
            : 30_000,
        ),
    });
  } catch (reason) {
    if (reason instanceof DOMException && reason.name === "TimeoutError") {
      throw new Error(
        init.body instanceof FormData
          ? "The upload request timed out. Check that FastAPI is running and the Supabase Storage bucket is configured, then try again."
          : "The backend did not respond in time. Check that FastAPI is running on port 8000.",
      );
    }
    if (isAbortError(reason)) {
      throw new Error(userFacingError(reason));
    }
    if (reason instanceof TypeError) {
      const requestUrl = new URL(apiUrl(path), window.location.origin);
      const isLocal = ["localhost", "127.0.0.1"].includes(
        window.location.hostname,
      );
      throw new Error(
        isLocal
          ? `Cannot connect to the API at ${requestUrl.origin}. Start FastAPI from the project root with: python -m uvicorn backend.app.main:app --reload --port 8000`
          : `The API request could not reach ${requestUrl.origin}. Check the service status and request logs in Render.`,
      );
    }
    throw reason;
  }
  const contentType = response.headers.get("content-type") || "";
  const requestUrl = new URL(apiUrl(path), window.location.origin);
  const isHtmlResponse = contentType.includes("text/html");
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    if (contentType.includes("application/json")) {
      try {
        detail = (await response.json()).detail || detail;
      } catch {
        /* Invalid JSON error body. */
      }
    } else if (isHtmlResponse) {
      detail = `The API request to ${requestUrl.origin}${requestUrl.pathname} returned an HTML page (HTTP ${response.status}) instead of a FastAPI response. Check that this origin is the backend service. If the frontend and backend are separate, set VITE_API_URL to the backend origin and redeploy the frontend; if they share a Render service, hard-refresh the page and confirm the latest backend deployment is live.`;
    }
    if (response.status === 404 && !isHtmlResponse) {
      const requestPath = requestUrl.pathname;
      const isLectureDetail = /^\/api\/lectures\/[^/]+$/.test(requestPath);
      detail = isLectureDetail
        ? `Lecture not found at ${requestPath}. It may have been deleted, or it may belong to another account. If it should exist, refresh the library and check that the frontend and backend deployments are up to date.`
        : `API route or resource not found at ${requestPath}. Confirm that the frontend and backend deployment versions match.`;
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  if (!contentType.includes("application/json")) {
    throw new Error(
      `The API request to ${requestUrl.origin}${requestUrl.pathname} returned a non-JSON response. Check VITE_API_URL and make sure the FastAPI backend is running.`,
    );
  }
  return response.json() as Promise<T>;
}

function useTheme() {
  const [theme, setTheme] = useState<"light" | "dark">(
    () =>
      (localStorage.getItem("notesgraph-theme") as "light" | "dark") || "light",
  );
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("notesgraph-theme", theme);
  }, [theme]);
  return {
    theme,
    toggle: () => setTheme(theme === "light" ? "dark" : "light"),
  };
}

function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const theme = useTheme();
  useEffect(() => {
    if (!supabase) {
      setAuthLoading(false);
      return;
    }
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setAuthLoading(false);
    });
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, next) => setSession(next));
    return () => subscription.unsubscribe();
  }, []);

  return (
    <Routes>
      <Route path="/" element={<Landing theme={theme} />} />
      <Route
        path="/sign-in"
        element={session ? <Navigate to="/app" replace /> : <SignIn />}
      />
      <Route
        path="/app/*"
        element={
          authLoading ? (
            <LoadingScreen />
          ) : session ? (
            <Dashboard key={session.user.id} session={session} theme={theme} />
          ) : (
            <Navigate to="/sign-in" replace />
          )
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <Link to="/" className="brand-lockup" aria-label="NotesGraph home">
      <span className="brand-mark">
        <AudioLines size={24} strokeWidth={2.2} />
      </span>
      {!compact && (
        <span className="brand-name">
          <span>Notes</span>
          <span className="brand-accent">Graph</span>
        </span>
      )}
    </Link>
  );
}

function ThemeButton({
  theme,
  toggle,
}: {
  theme: ReturnType<typeof useTheme>["theme"];
  toggle: () => void;
}) {
  return (
    <button
      className="icon-btn theme-toggle"
      aria-label={`Switch to ${theme === "light" ? "dark" : "light"} mode`}
      onClick={toggle}
    >
      {theme === "light" ? <Moon size={18} /> : <Sun size={18} />}
    </button>
  );
}

function Landing({ theme }: { theme: ReturnType<typeof useTheme> }) {
  const [splash, setSplash] = useState(
    !sessionStorage.getItem("notesgraph-splashed"),
  );
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
        {splash ? (
          <motion.div
            key="splash"
            className="splash"
            initial={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.45 }}
          >
            <motion.div
              className="splash-logo"
              initial={{ scale: 0.8, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              transition={{ duration: 0.6, ease: "easeOut" }}
            >
              <Brand />
              <span className="splash-orbit" />
            </motion.div>
          </motion.div>
        ) : (
          <motion.div
            key="home"
            className="landing-page"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.55 }}
          >
            <header className="site-nav shell">
              <Brand />
              <nav>
                <a href="#how">How it works</a>
                <a href="#features">Features</a>
              </nav>
              <div className="nav-actions">
                <ThemeButton {...theme} />
                <Link className="nav-login" to="/sign-in">
                  Sign in
                </Link>
                <Link className="button button-dark" to="/sign-in">
                  Get started <ArrowRight size={16} />
                </Link>
              </div>
            </header>
            <section className="hero shell">
              <motion.div
                className="hero-copy"
                initial={{ opacity: 0, y: 18 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.12 }}
              >
                <span className="eyebrow">
                  <span className="eyebrow-dot" /> YOUR LECTURES, UNDERSTOOD
                </span>
                <h1>
                  Make every lecture
                  <br />a <em>lasting advantage.</em>
                </h1>
                <p>
                  Turn recordings into thoughtful notes, then ask anything and
                  jump straight to the moment it was taught.
                </p>
                <div className="hero-cta">
                  <Link
                    to="/sign-in"
                    className="button button-dark button-large"
                  >
                    Start learning free <ArrowRight size={17} />
                  </Link>
                  <span>No credit card needed</span>
                </div>
                <div className="hero-proof">
                  <div className="avatar-stack">
                    <i>J</i>
                    <i>A</i>
                    <i>M</i>
                    <i>+</i>
                  </div>
                  <span>
                    Built for curious minds
                    <br />
                    <b>and busy semesters</b>
                  </span>
                </div>
              </motion.div>
              <motion.div
                className="hero-art"
                initial={{ opacity: 0, scale: 0.94, rotate: 2 }}
                animate={{ opacity: 1, scale: 1, rotate: 0 }}
                transition={{ delay: 0.2, duration: 0.7 }}
              >
                <div className="art-glow" />
                <div className="lecture-card">
                  <div className="lecture-card-top">
                    <span className="tiny-file">
                      <FileAudio2 size={16} />
                    </span>
                    <span>LECTURE 08 · 42 MIN</span>
                    <span className="live-dot" />
                  </div>
                  <h3>Operating Systems</h3>
                  <p>Deadlocks &amp; resource allocation</p>
                  <div className="waveform">
                    {Array.from({ length: 44 }, (_, i) => (
                      <i
                        key={i}
                        style={{ height: `${12 + ((i * 37 + 13) % 50)}px` }}
                      />
                    ))}
                  </div>
                  <div className="art-progress">
                    <span />
                    <small>18:42</small>
                    <small>42:08</small>
                  </div>
                  <div className="art-note">
                    <span className="note-spark">
                      <Sparkles size={14} />
                    </span>
                    <div>
                      <b>Key idea found</b>
                      <span>Four conditions for deadlock</span>
                    </div>
                    <span className="time-pill">18:42</span>
                  </div>
                </div>
                <div className="floating-tag">
                  <Clock3 size={15} /> Answers with timestamps
                </div>
                <div className="floating-note">
                  <BookOpen size={16} />
                  <span>Notes, neatly organized</span>
                  <Check size={15} />
                </div>
              </motion.div>
            </section>
            <section className="trust-strip shell">
              <span>ONE PLACE FOR THE WHOLE LEARNING LOOP</span>
              <div>
                <i>
                  <AudioLines size={17} /> Listen
                </i>
                <b>→</b>
                <i>
                  <Sparkles size={17} /> Understand
                </i>
                <b>→</b>
                <i>
                  <MessageCircle size={17} /> Ask
                </i>
              </div>
            </section>
            <section className="feature-section shell" id="features">
              <span className="eyebrow">LESS REPLAYING. MORE LEARNING.</span>
              <h2>Every detail, right where you need it.</h2>
              <div className="feature-grid">
                <article>
                  <span className="feature-icon mint">
                    <FileText />
                  </span>
                  <h3>Notes that make sense</h3>
                  <p>
                    Key concepts, announcements and exam hints, organized from
                    the whole lecture.
                  </p>
                </article>
                <article>
                  <span className="feature-icon peach">
                    <Clock3 />
                  </span>
                  <h3>Answers with a way back</h3>
                  <p>
                    Ask a question and get a helpful answer linked to the exact
                    moment in the audio.
                  </p>
                </article>
                <article>
                  <span className="feature-icon lilac">
                    <FolderOpen />
                  </span>
                  <h3>A calmer course library</h3>
                  <p>
                    Subjects become folders. Your notes and chat history stay
                    together and private.
                  </p>
                </article>
              </div>
            </section>
            <section className="bottom-cta shell" id="how">
              <div>
                <span className="eyebrow">YOUR NEXT CLASS CAN START HERE</span>
                <h2>
                  Learn more from the time
                  <br />
                  you already spend listening.
                </h2>
              </div>
              <Link to="/sign-in" className="button button-dark button-large">
                Create your free space <ArrowRight size={17} />
              </Link>
            </section>
            <footer className="site-footer shell">
              <Brand />
              <span>Your learning, in context.</span>
              <span>© NotesGraph</span>
            </footer>
          </motion.div>
        )}
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
    if (!supabase) {
      setError(supabaseConfigError);
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result =
        mode === "login"
          ? await supabase.auth.signInWithPassword({ email, password })
          : await supabase.auth.signUp({ email, password });
      if (result.error) {
        setError(
          result.error.message.toLowerCase().includes("invalid api key")
            ? "Supabase rejected the API key. Use this project's sb_publishable_ key in frontend/.env as VITE_SUPABASE_PUBLISHABLE_KEY, then restart Vite."
            : result.error.message,
        );
        return;
      }
      if (mode === "signup" && !result.data.session) {
        setNotice("Check your inbox to confirm your email, then sign in.");
        return;
      }
      navigate("/app");
    } catch (reason) {
      setError(userFacingError(reason, "Unable to reach Supabase Auth."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="auth-page">
      <Link to="/" className="auth-back">
        <ArrowRight size={15} className="back-arrow" /> Back to home
      </Link>
      <div className="auth-art">
        <div className="auth-art-grid" />
        <div className="auth-art-copy">
          <Brand />
          <div>
            <span className="eyebrow">A BETTER WAY TO REVISE</span>
            <h1>
              Your classes,
              <br />
              made <em>clear.</em>
            </h1>
            <p>Everything you learned, organized and ready when you need it.</p>
          </div>
          <div className="auth-art-foot">
            <span>Private by design</span>
            <span>·</span>
            <span>Made for learners</span>
          </div>
        </div>
      </div>
      <section className="auth-form-wrap">
        <div className="auth-form">
          <span className="eyebrow">
            {mode === "login" ? "WELCOME BACK" : "MAKE SPACE TO LEARN"}
          </span>
          <h2>
            {mode === "login" ? "Sign in to your space" : "Create your account"}
          </h2>
          <p>
            {mode === "login"
              ? "Your notes and conversations are right where you left them."
              : "A more useful way to keep up with every class."}
          </p>
          <div className="auth-tabs">
            <button
              className={mode === "login" ? "active" : ""}
              onClick={() => setMode("login")}
            >
              Sign in
            </button>
            <button
              className={mode === "signup" ? "active" : ""}
              onClick={() => setMode("signup")}
            >
              Create account
            </button>
          </div>
          <form onSubmit={submit}>
            <label>
              Email address
              <input
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="you@school.edu"
              />
            </label>
            <label>
              Password
              <div className="password-input-wrap">
                <input
                  type={showPassword ? "text" : "password"}
                  autoComplete={
                    mode === "login" ? "current-password" : "new-password"
                  }
                  minLength={8}
                  required
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="At least 8 characters"
                />
                <button
                  className="password-visibility-toggle"
                  type="button"
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  aria-pressed={showPassword}
                  onClick={() => setShowPassword((visible) => !visible)}
                >
                  {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
            </label>
            {error && <div className="form-alert">{error}</div>}
            {notice && <div className="form-notice">{notice}</div>}
            <button className="button button-dark auth-submit" disabled={busy}>
              {busy ? <LoaderCircle className="spin" size={17} /> : null}
              {mode === "login" ? "Sign in" : "Create account"}{" "}
              <ArrowRight size={16} />
            </button>
          </form>
          <small className="privacy-note">
            <span>🔒</span> Your recordings and notes are private to your
            account.
          </small>
        </div>
      </section>
    </main>
  );
}

function LoadingScreen() {
  return (
    <div className="loading-screen">
      <span className="brand-mark">
        <AudioLines size={19} />
      </span>
      <LoaderCircle className="spin" size={21} />
    </div>
  );
}

function Dashboard({
  session,
  theme,
}: {
  session: Session;
  theme: ReturnType<typeof useTheme>;
}) {
  const navigate = useNavigate();
  const lectureRoute = useMatch("/app/lectures/:lectureId");
  const routeLectureId = lectureRoute?.params.lectureId;
  const lectureCacheKey = `notesgraph-lectures:${session.user.id}`;
  const folderCacheKey = `notesgraph-folders:${session.user.id}`;
  const [lectures, setLectures] = useState<Lecture[]>(
    () => readCache<Lecture[]>(lectureCacheKey) || [],
  );
  const [folders, setFolders] = useState<Folder[]>(
    () => readCache<Folder[]>(folderCacheKey) || [],
  );
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
  const [resumeUpload, setResumeUpload] = useState<Lecture | null>(null);
  const [uploadProgress, setUploadProgress] = useState<
    Record<string, number | null>
  >({});
  const [showSubjectInput, setShowSubjectInput] = useState(false);
  const [newSubject, setNewSubject] = useState("");
  const [oldestFirst, setOldestFirst] = useState(false);
  const [mobileMenu, setMobileMenu] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(
    () => readCache<Lecture[]>(lectureCacheKey) === null,
  );
  const [detailLoading, setDetailLoading] = useState(false);
  const userName = session.user.email?.split("@")[0] || "Learner";

  const refresh = useCallback(async () => {
    const [lectureRows, folderRows] = await Promise.all([
      api<Lecture[]>("/lectures"),
      api<Folder[]>("/folders"),
    ]);
    setLectures(lectureRows);
    setFolders(folderRows);
    writeCache(lectureCacheKey, lectureRows);
    writeCache(folderCacheKey, folderRows);
    return lectureRows;
  }, [lectureCacheKey, folderCacheKey]);

  useEffect(() => {
    let cancelled = false;
    refresh()
      .catch((reason) => {
        if (!cancelled) setError(userFacingError(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  const openLecture = useCallback(
    async (lectureId: string) => {
      navigate(`/app/lectures/${lectureId}`);
    },
    [navigate],
  );

  useEffect(() => {
    chatLoadRequest.current += 1;
    setSelected(null);
    setSessions([]);
    setChatSession(null);
    setMessages([]);
    setView("notes");
    setError("");
    if (!routeLectureId) {
      setDetailLoading(false);
      return;
    }

    let cancelled = false;
    setDetailLoading(true);
    const loadLecture = async () => {
      try {
        const [data, rows] = await Promise.all([
          api<Lecture>(`/lectures/${routeLectureId}`),
          api<ChatSession[]>(`/chat-sessions?lecture_id=${routeLectureId}`),
        ]);
        if (cancelled) return;
        setSelected(data);
        setSessions(rows);
      } catch (reason) {
        if (!cancelled) setError(userFacingError(reason));
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    };
    void loadLecture();
    return () => {
      cancelled = true;
    };
  }, [routeLectureId]);

  const activeLectureIds = useMemo(
    () =>
      lectures
        .filter((item) =>
          ["queued", "transcribing", "generating_notes", "indexing"].includes(
            item.status,
          ),
        )
        .map((item) => item.id)
        .join(","),
    [lectures],
  );
  useEffect(() => {
    if (!activeLectureIds) return;
    let cancelled = false;
    let timer: number | undefined;
    const ids = activeLectureIds.split(",");
    const poll = async () => {
      const results = await Promise.all(
        ids.map(async (id) => {
          try {
            return await api<Lecture>(`/lectures/${id}`);
          } catch (reason) {
            if (!cancelled) setError(userFacingError(reason));
            return null;
          }
        }),
      );
      if (cancelled) return;
      const updated = results.filter((item): item is Lecture => item !== null);
      if (updated.length) {
        setLectures((current) =>
          current.map(
            (item) => updated.find((row) => row.id === item.id) || item,
          ),
        );
        setSelected((current) =>
          current
            ? updated.find((row) => row.id === current.id) || current
            : current,
        );
        if (
          updated.some((item) =>
            ["done", "error", "index_error"].includes(item.status),
          )
        ) {
          try {
            await refresh();
          } catch (reason) {
            if (!cancelled) setError(userFacingError(reason));
          }
        }
      }
      if (!cancelled) timer = window.setTimeout(poll, 2500);
    };
    timer = window.setTimeout(poll, 1500);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [activeLectureIds, refresh]);

  const filteredLectures = useMemo(
    () =>
      lectures
        .filter(
          (item) =>
            (!folderFilter || item.folder_id === folderFilter) &&
            `${item.title} ${item.subject}`
              .toLowerCase()
              .includes(query.toLowerCase()),
        )
        .sort((left, right) => {
          const difference =
            Date.parse(left.created_at) - Date.parse(right.created_at);
          return oldestFirst ? difference : -difference;
        }),
    [lectures, folderFilter, query, oldestFirst],
  );

  async function createSubject(event: FormEvent) {
    event.preventDefault();
    const name = newSubject.trim();
    if (!name) return;
    try {
      await api<Folder>("/folders", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      setNewSubject("");
      setShowSubjectInput(false);
      await refresh();
    } catch (reason) {
      setError(userFacingError(reason));
    }
  }

  async function chooseChat(chat: ChatSession, loadMessages = true) {
    setChatSession(chat);
    setView("chat");
    setError("");
    setSessions((current) =>
      current.some((item) => item.id === chat.id)
        ? current.map((item) => (item.id === chat.id ? chat : item))
        : [chat, ...current],
    );
    if (!loadMessages) {
      chatLoadRequest.current += 1;
      return;
    }
    const requestId = ++chatLoadRequest.current;
    setMessages([]);
    try {
      const rows = await api<ChatMessage[]>(
        `/chat-sessions/${chat.id}/messages`,
      );
      if (requestId === chatLoadRequest.current) setMessages(rows);
    } catch (reason) {
      if (requestId === chatLoadRequest.current)
        setError(userFacingError(reason));
    }
  }

  async function createChat() {
    if (!selected) return;
    try {
      const chat = await api<ChatSession>(
        `/lectures/${selected.id}/chat-sessions`,
        { method: "POST" },
      );
      setSessions((current) => [chat, ...current]);
      setChatSession(chat);
      setMessages([]);
      setView("chat");
    } catch (reason) {
      setError(userFacingError(reason));
    }
  }

  async function deleteLecture(id: string) {
    if (
      !window.confirm("Delete this lecture, its notes, and its chat history?")
    )
      return;
    try {
      await api(`/lectures/${id}`, { method: "DELETE" });
      navigate("/app");
      await refresh();
    } catch (reason) {
      setError(userFacingError(reason));
    }
  }

  async function retryLecture(id: string) {
    try {
      const updated = await api<Lecture>(`/lectures/${id}/retry`, {
        method: "POST",
      });
      setSelected((current) =>
        current?.id === id ? { ...current, ...updated } : current,
      );
      setLectures((current) =>
        current.map((item) =>
          item.id === id ? { ...item, ...updated } : item,
        ),
      );
      await refresh();
    } catch (reason) {
      setError(userFacingError(reason));
    }
  }

  async function retryIndex(id: string) {
    try {
      const updated = await api<Lecture>(`/lectures/${id}/retry-index`, {
        method: "POST",
      });
      setSelected((current) =>
        current?.id === id ? { ...current, ...updated } : current,
      );
      setLectures((current) =>
        current.map((item) =>
          item.id === id ? { ...item, ...updated } : item,
        ),
      );
      await refresh();
    } catch (reason) {
      setError(userFacingError(reason));
    }
  }

  function onLectureCreated(lecture: Lecture, uploadPending: boolean) {
    setShowUpload(false);
    setResumeUpload(null);
    navigate("/app");
    setLectures((current) => [
      lecture,
      ...current.filter((item) => item.id !== lecture.id),
    ]);
    if (uploadPending) {
      setUploadProgress((current) => ({ ...current, [lecture.id]: 0 }));
    }
    setError("");
    void refresh().catch((reason) => setError(userFacingError(reason)));
  }

  function onUploadProgress(lectureId: string, percentage: number | null) {
    setUploadProgress((current) => ({ ...current, [lectureId]: percentage }));
  }

  function onUploadComplete(lecture: Lecture) {
    setUploadProgress((current) => {
      const next = { ...current };
      delete next[lecture.id];
      return next;
    });
    setLectures((current) =>
      current.map((item) => (item.id === lecture.id ? lecture : item)),
    );
    setSelected((current) =>
      current?.id === lecture.id ? { ...current, ...lecture } : current,
    );
    void refresh().catch((reason) => setError(userFacingError(reason)));
  }

  function onUploadFailed(lectureId: string, message: string) {
    setUploadProgress((current) => {
      const next = { ...current };
      delete next[lectureId];
      return next;
    });
    void (async () => {
      try {
        const failed = await api<Lecture>(
          `/lectures/${lectureId}/upload-failed`,
          {
            method: "POST",
            body: JSON.stringify({ error: message }),
          },
        );
        setLectures((current) =>
          current.map((item) => (item.id === lectureId ? failed : item)),
        );
        setSelected((current) =>
          current?.id === lectureId ? { ...current, ...failed } : current,
        );
        setError(message);
      } catch (reason) {
        try {
          const current = await api<Lecture>(`/lectures/${lectureId}`);
          setLectures((rows) =>
            rows.map((item) => (item.id === lectureId ? current : item)),
          );
          setSelected((lecture) =>
            lecture?.id === lectureId ? { ...lecture, ...current } : lecture,
          );
          if (
            [
              "queued",
              "transcribing",
              "generating_notes",
              "indexing",
              "done",
              "index_error",
            ].includes(current.status)
          ) {
            return;
          }
        } catch (refreshReason) {
          setError(
            userFacingError(
              refreshReason,
              "The upload failed and its status could not be saved.",
            ),
          );
          return;
        }
        setError(
          userFacingError(
            reason,
            "The upload failed and its status could not be saved.",
          ),
        );
      }
    })();
  }

  function resumeAudioUpload(lecture: Lecture) {
    setResumeUpload(lecture);
    setShowUpload(true);
  }

  async function showTranscript() {
    if (!selected) return;
    setView("transcript");
    setTranscript("");
    try {
      if (!supabase) throw new Error(supabaseConfigError);
      const {
        data: { session: active },
      } = await supabase.auth.getSession();
      const response = await fetch(
        apiUrl(`/lectures/${selected.id}/transcript`),
        { headers: { Authorization: `Bearer ${active?.access_token}` } },
      );
      if (!response.ok) throw new Error("Transcript is not ready yet.");
      setTranscript(await response.text());
    } catch (reason) {
      setError(userFacingError(reason));
    }
  }

  async function download(path: string, filename: string) {
    try {
      if (!supabase) throw new Error(supabaseConfigError);
      const {
        data: { session: active },
      } = await supabase.auth.getSession();
      const response = await fetch(apiUrl(path), {
        headers: { Authorization: `Bearer ${active?.access_token}` },
      });
      if (!response.ok) throw new Error("Unable to download this file.");
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(userFacingError(reason));
    }
  }

  async function signOut() {
    localStorage.removeItem(lectureCacheKey);
    localStorage.removeItem(folderCacheKey);
    await supabase?.auth.signOut();
    navigate("/");
  }

  return (
    <div className="workspace">
      <aside className={`sidebar ${mobileMenu ? "sidebar-open" : ""}`}>
        <div className="sidebar-head">
          <Brand />
          <button
            className="icon-btn mobile-close"
            onClick={() => setMobileMenu(false)}
          >
            <X size={18} />
          </button>
        </div>
        <button
          className="new-lecture"
          onClick={() => {
            setResumeUpload(null);
            setShowUpload(true);
          }}
        >
          <Plus size={18} /> New lecture <span>⌘ K</span>
        </button>
        <nav className="side-nav">
          <button
            className={!folderFilter ? "side-link selected" : "side-link"}
            onClick={() => {
              setFolderFilter(null);
              navigate("/app");
            }}
          >
            <LayoutDashboard size={17} /> All lectures{" "}
            <span>{lectures.length}</span>
          </button>
          <div className="side-label">
            YOUR SUBJECTS{" "}
            <button
              type="button"
              title="Create a subject"
              aria-label="Create a subject"
              onClick={() => setShowSubjectInput((value) => !value)}
            >
              <Plus size={14} />
            </button>
          </div>
          {showSubjectInput && (
            <form className="subject-create" onSubmit={createSubject}>
              <input
                autoFocus
                value={newSubject}
                onChange={(event) => setNewSubject(event.target.value)}
                placeholder="Subject name"
                aria-label="Subject name"
                maxLength={100}
              />
              <button
                type="submit"
                disabled={!newSubject.trim()}
                aria-label="Save subject"
              >
                <Check size={15} />
              </button>
            </form>
          )}
          {folders.map((folder) => (
            <button
              key={folder.id}
              className={
                folderFilter === folder.id ? "side-link selected" : "side-link"
              }
              onClick={() => {
                setFolderFilter(folder.id);
                navigate("/app");
              }}
            >
              <FolderClosed size={16} />
              {folder.name}
              <span>
                {lectures.filter((item) => item.folder_id === folder.id).length}
              </span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="private-card">
            <span className="private-icon">
              <Sparkles size={16} />
            </span>
            <div>
              <b>Your private library</b>
              <small>Only you can see your content.</small>
            </div>
          </div>
          <div className="profile-row">
            <span className="profile-avatar">
              {userName.slice(0, 1).toUpperCase()}
            </span>
            <div className="profile-name">
              <b>{userName}</b>
              <small>Personal workspace</small>
            </div>
            <button className="icon-btn" title="Sign out" onClick={signOut}>
              <LogOut size={17} />
            </button>
          </div>
        </div>
      </aside>
      {mobileMenu && (
        <button
          className="mobile-backdrop"
          onClick={() => setMobileMenu(false)}
          aria-label="Close menu"
        />
      )}
      <main className="work-main">
        <header className="work-topbar">
          <button
            className="icon-btn mobile-menu"
            onClick={() => setMobileMenu(true)}
          >
            <Menu size={19} />
          </button>
          <div className="breadcrumbs">
            <span>My workspace</span>
            <span>/</span>
            <b>
              {selected?.title ||
                folders.find((folder) => folder.id === folderFilter)?.name ||
                "All lectures"}
            </b>
          </div>
          <div className="work-actions">
            <span className="secure-tag">
              <span /> Private workspace
            </span>
            <ThemeButton {...theme} />
            <span className="profile-avatar top-avatar">
              {userName.slice(0, 1).toUpperCase()}
            </span>
          </div>
        </header>
        <div className="work-content">
          {error && (
            <div className="error-banner">
              <span>{error}</span>
              <button onClick={() => setError("")}>
                <X size={15} />
              </button>
            </div>
          )}
          {routeLectureId && !selected && detailLoading ? (
            <div className="empty-state">
              <LoaderCircle className="spin" />
            </div>
          ) : !selected ? (
            <section className="library-view">
              <div className="welcome-row">
                <div>
                  <span className="eyebrow">YOUR LEARNING SPACE</span>
                  <h1>
                    {folderFilter
                      ? folders.find((folder) => folder.id === folderFilter)
                          ?.name
                      : `Good to see you, ${userName}`}
                  </h1>
                  <p>
                    {folderFilter
                      ? "Your course notes and lecture recordings in one place."
                      : "Pick up where you left off, or make something new."}
                  </p>
                </div>
                <button
                  className="button button-dark"
                  onClick={() => {
                    setResumeUpload(null);
                    setShowUpload(true);
                  }}
                >
                  <Plus size={17} /> Add a lecture
                </button>
              </div>
              <div className="library-toolbar">
                <div className="searchbox">
                  <Search size={16} />
                  <input
                    placeholder="Search lectures..."
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                  />
                </div>
                <button
                  className="sort-button"
                  onClick={() => setOldestFirst((value) => !value)}
                  aria-label="Change lecture sorting"
                >
                  {oldestFirst ? "Oldest first" : "Recently added"}{" "}
                  <ChevronDown size={15} />
                </button>
              </div>
              {loading ? (
                <div className="empty-state">
                  <LoaderCircle className="spin" />
                </div>
              ) : filteredLectures.length ? (
                <div className="lecture-grid">
                  {filteredLectures.map((lecture, index) => (
                    <motion.button
                      key={lecture.id}
                      className="library-card"
                      onClick={() => openLecture(lecture.id)}
                      initial={{ opacity: 0, y: 12 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: Math.min(index * 0.04, 0.25) }}
                    >
                      <span className={`course-cover cover-${index % 4}`}>
                        <AudioLines size={24} />
                        <span>{lecture.subject || "LECTURE"}</span>
                      </span>
                      <span className="library-card-info">
                        <b>{lecture.title}</b>
                        <small>
                          <span>{lecture.subject || "Uncategorized"}</span>
                          <span>·</span>
                          <span>
                            {new Date(lecture.created_at).toLocaleDateString()}
                          </span>
                        </small>
                        <span
                          className={`status-chip ${lecture.status === "done" || lecture.status === "index_error" ? "ready" : lecture.status === "error" || lecture.status === "upload_error" ? "failed" : "processing"}`}
                        >
                          <i />
                          {lecture.status === "done"
                            ? "Notes ready"
                            : lecture.status === "index_error"
                              ? "Notes ready · chat needs retry"
                              : lecture.status === "error" ||
                                  lecture.status === "upload_error"
                                ? "Needs attention"
                                : lecture.status === "uploading" &&
                                    uploadProgress[lecture.id] !== undefined
                                  ? `Uploading audio${uploadProgress[lecture.id] === null ? "…" : ` · ${uploadProgress[lecture.id]}%`}`
                                  : lecture.status === "uploading"
                                    ? "Upload interrupted · open to resume"
                                    : lecture.progress || "Processing"}
                        </span>
                      </span>
                      <ArrowRight className="card-arrow" size={17} />
                    </motion.button>
                  ))}
                </div>
              ) : (
                <div className="empty-library">
                  <div className="empty-art">
                    <BookOpen size={27} />
                    <Sparkles size={16} />
                  </div>
                  <h3>
                    {query
                      ? "No lectures found"
                      : "Your first lecture starts here"}
                  </h3>
                  <p>
                    {query
                      ? "Try another search."
                      : "Add a recording or paste a transcript to create your first set of notes."}
                  </p>
                  {!query && (
                    <button
                      className="button button-dark"
                      onClick={() => {
                        setResumeUpload(null);
                        setShowUpload(true);
                      }}
                    >
                      <Plus size={17} /> Add your first lecture
                    </button>
                  )}
                </div>
              )}
              <div className="library-footnote">
                <span>
                  <Check size={14} /> Private to your account
                </span>
                <span>
                  {lectures.length}{" "}
                  {lectures.length === 1 ? "lecture" : "lectures"} saved
                </span>
              </div>
            </section>
          ) : (
            <LectureView
              lecture={selected}
              userInitial={userName.slice(0, 1).toUpperCase()}
              view={view}
              setView={setView}
              onBack={() => navigate("/app")}
              onDelete={() => deleteLecture(selected.id)}
              onRetry={() => retryLecture(selected.id)}
              onRetryIndex={() => retryIndex(selected.id)}
              onResumeUpload={() => resumeAudioUpload(selected)}
              uploadingAudio={uploadProgress[selected.id] !== undefined}
              onDownload={download}
              sessions={sessions}
              chatSession={chatSession}
              messages={messages}
              onCreateChat={createChat}
              onChooseChat={chooseChat}
              setMessages={setMessages}
              transcript={transcript}
              onTranscript={showTranscript}
            />
          )}
        </div>
      </main>
      <AnimatePresence>
        {showUpload && (
          <UploadModal
            onClose={() => setShowUpload(false)}
            resumeLecture={resumeUpload || undefined}
            onCreated={onLectureCreated}
            onUploadProgress={onUploadProgress}
            onUploadComplete={onUploadComplete}
            onUploadFailed={onUploadFailed}
          />
        )}
      </AnimatePresence>
    </div>
  );
}

function LectureView({
  lecture,
  userInitial,
  view,
  setView,
  onBack,
  onDelete,
  onRetry,
  onRetryIndex,
  onResumeUpload,
  uploadingAudio,
  onDownload,
  sessions,
  chatSession: active,
  messages,
  onCreateChat: onCreate,
  onChooseChat: onChoose,
  setMessages,
  transcript,
  onTranscript,
}: {
  lecture: Lecture;
  userInitial: string;
  view: "notes" | "chat" | "transcript";
  setView: (view: "notes" | "chat" | "transcript") => void;
  onBack: () => void;
  onRetry: () => void;
  onRetryIndex: () => void;
  onResumeUpload: () => void;
  uploadingAudio: boolean;
  onDelete: () => void;
  onDownload: (path: string, filename: string) => void;
  sessions: ChatSession[];
  chatSession: ChatSession | null;
  messages: ChatMessage[];
  onCreateChat: () => void;
  onChooseChat: (session: ChatSession, loadMessages?: boolean) => void;
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
  transcript: string;
  onTranscript: () => void;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [audioUrl, setAudioUrl] = useState("");
  const [audioError, setAudioError] = useState("");

  useEffect(() => {
    if (!lecture.audio_path || !supabase) {
      setAudioUrl("");
      setAudioError("");
      return;
    }
    let active = true;
    setAudioUrl("");
    setAudioError("");
    supabase.storage
      .from("lecture-audio")
      .createSignedUrl(lecture.audio_path, 3600)
      .then(({ data, error }) => {
        if (error) throw error;
        if (active) {
          setAudioUrl(data.signedUrl);
          setAudioError("");
        }
      })
      .catch((reason) => {
        if (active) setAudioError(userFacingError(reason));
      });
    return () => {
      active = false;
    };
  }, [lecture.audio_path]);

  const onSeek = useCallback((startSeconds: number) => {
    const audio = audioRef.current;
    if (!audio) {
      setAudioError("The lecture audio is not ready to play yet.");
      return;
    }
    setAudioError("");
    audio.currentTime = Math.max(0, startSeconds - 1.5);
    void audio.play().catch((reason) => setAudioError(userFacingError(reason)));
  }, []);

  return (
    <motion.section
      className="lecture-detail"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
    >
      <button className="back-library" onClick={onBack}>
        <ArrowRight className="back-arrow" size={15} /> Library
      </button>
      <div className="detail-heading">
        <div>
          <span className="eyebrow">{lecture.subject || "LECTURE NOTES"}</span>
          <h1>{lecture.title}</h1>
          <p>
            {lecture.source} <span>·</span> Added{" "}
            {new Date(lecture.created_at).toLocaleDateString()}
          </p>
        </div>
        <div className="detail-actions">
          {(lecture.status === "done" || lecture.status === "index_error") && (
            <>
              <button
                className="button button-outline"
                onClick={() =>
                  onDownload(
                    `/lectures/${lecture.id}/notes.pdf`,
                    `${lecture.title}.pdf`,
                  )
                }
              >
                <ArrowDownToLine size={16} /> PDF
              </button>
              <button
                className="button button-outline"
                onClick={() =>
                  onDownload(
                    `/lectures/${lecture.id}/notes.md`,
                    `${lecture.title}.md`,
                  )
                }
              >
                <ArrowDownToLine size={16} /> Markdown
              </button>
            </>
          )}
          {lecture.status === "error" && (
            <button className="button button-outline" onClick={onRetry}>
              Retry processing
            </button>
          )}
          {lecture.status === "index_error" && (
            <button className="button button-outline" onClick={onRetryIndex}>
              Retry chat indexing
            </button>
          )}
          {(lecture.status === "uploading" ||
            lecture.status === "upload_error") &&
            lecture.audio_path &&
            !uploadingAudio && (
              <button
                className="button button-outline"
                onClick={onResumeUpload}
              >
                Resume audio upload
              </button>
            )}
          {(lecture.status === "done" ||
            lecture.status === "error" ||
            lecture.status === "upload_error" ||
            lecture.status === "index_error") && (
            <button
              className="icon-btn danger-icon"
              title="Delete lecture"
              onClick={onDelete}
            >
              <Trash2 size={17} />
            </button>
          )}
        </div>
      </div>
      {lecture.status !== "done" && lecture.status !== "index_error" && (
        <div
          className={`process-banner ${lecture.status === "error" || lecture.status === "upload_error" ? "process-error" : ""}`}
        >
          {lecture.status === "error" || lecture.status === "upload_error" ? (
            <span>
              {lecture.error || "Something went wrong while processing."}
            </span>
          ) : (
            <>
              <LoaderCircle className="spin" size={18} />
              <div>
                <b>{lecture.progress || "Getting your notes ready"}</b>
                <small>
                  {lecture.status === "uploading"
                    ? "The recording upload can be resumed from here after a refresh."
                    : "You can leave this page; processing continues in the background."}
                </small>
              </div>
            </>
          )}
        </div>
      )}
      {lecture.status === "index_error" && (
        <div className="process-banner process-error">
          {lecture.error ||
            "Your notes are ready. Chat indexing can be retried without regenerating them."}
        </div>
      )}
      {(lecture.status === "done" || lecture.status === "index_error") && (
        <div className="detail-layout">
          <div className="detail-primary">
            {audioUrl && (
              <div className="lecture-audio">
                <audio
                  ref={audioRef}
                  controls
                  preload="metadata"
                  src={audioUrl}
                >
                  Your browser does not support audio playback.
                </audio>
              </div>
            )}
            {audioError && <p className="audio-error">{audioError}</p>}
            <div className="detail-tabs">
              <button
                className={view === "notes" ? "active" : ""}
                onClick={() => setView("notes")}
              >
                <FileText size={16} /> Study notes
              </button>
              <button
                className={view === "chat" ? "active" : ""}
                onClick={() => setView("chat")}
              >
                <MessageCircle size={16} /> Ask this lecture
              </button>
              <button
                className={view === "transcript" ? "active" : ""}
                onClick={onTranscript}
              >
                <AudioLines size={16} /> Transcript
              </button>
            </div>
            {view === "notes" && <NotesView notes={lecture.notes} />}
            {view === "chat" && (
              <ChatView
                lectureId={lecture.id}
                userInitial={userInitial}
                sessions={sessions}
                active={active}
                messages={messages}
                onCreate={onCreate}
                onChoose={onChoose}
                setMessages={setMessages}
                onSeek={onSeek}
              />
            )}
            {view === "transcript" && (
              <div className="transcript-panel">
                <div className="panel-title">
                  <div>
                    <span className="eyebrow">FROM THE RECORDING</span>
                    <h2>Lecture transcript</h2>
                  </div>
                  <span className="transcript-readonly">
                    <Check size={14} /> Source transcript
                  </span>
                </div>
                <pre>{transcript || "Loading transcript…"}</pre>
              </div>
            )}
          </div>
          <aside className="detail-aside">
            <div className="aside-card">
              <span className="aside-icon">
                <Sparkles size={17} />
              </span>
              <span className="eyebrow">QUICK OVERVIEW</span>
              <p>
                {lecture.notes?.summary ||
                  "Your lecture summary will appear here."}
              </p>
              <div className="aside-stats">
                <span>
                  <b>{lecture.notes?.core_explanations?.length || 0}</b> topics
                </span>
                <span>
                  <b>{lecture.notes?.exam_points?.length || 0}</b> exam tips
                </span>
              </div>
            </div>
            <div className="aside-card chat-history-card">
              <div className="history-heading">
                <span className="eyebrow">CHAT HISTORY</span>
                <button title="Start a new chat" onClick={onCreate}>
                  <Plus size={16} />
                </button>
              </div>
              {sessions.length ? (
                sessions.map((chat) => (
                  <button
                    key={chat.id}
                    className={`history-item ${active?.id === chat.id ? "current" : ""}`}
                    onClick={() => onChoose(chat)}
                  >
                    <MessageCircle size={15} />
                    <span>{chat.title}</span>
                    <ArrowRight size={13} />
                  </button>
                ))
              ) : (
                <p className="history-empty">
                  Your questions about this lecture will stay here.
                </p>
              )}
            </div>
          </aside>
        </div>
      )}
    </motion.section>
  );
}

function NotesView({ notes }: { notes?: Notes }) {
  if (!notes)
    return (
      <div className="empty-library">
        <h3>Notes are being prepared</h3>
        <p>They will appear here when the lecture is ready.</p>
      </div>
    );
  return (
    <article className="notes-page">
      <div className="notes-intro">
        <span className="eyebrow">LECTURE SUMMARY</span>
        <p>{notes.summary}</p>
      </div>
      <NoteSection
        title="Core explanations"
        tone="blue"
        icon={<BookOpen size={17} />}
      >
        {notes.core_explanations.length ? (
          notes.core_explanations.map((topic, index) => (
            <div className="concept" key={`${topic.topic}-${index}`}>
              <h3>{topic.topic}</h3>
              <p>{topic.explanation}</p>
              {topic.key_points?.length > 0 && (
                <ul>
                  {topic.key_points.map((point) => (
                    <li key={point}>{point}</li>
                  ))}
                </ul>
              )}
            </div>
          ))
        ) : (
          <p className="empty-note">No academic content was identified.</p>
        )}
      </NoteSection>
      <NoteSection
        title="Exam-relevant points"
        tone="rose"
        icon={<Sparkles size={17} />}
      >
        {notes.exam_points.length ? (
          notes.exam_points.map((item, index) => (
            <div className="exam-note" key={index}>
              <span>{item.kind.replaceAll("_", " ")}</span>
              {item.point}
            </div>
          ))
        ) : (
          <p className="empty-note">No exam hints were identified.</p>
        )}
      </NoteSection>
      <NoteSection
        title="Announcements"
        tone="amber"
        icon={<Clock3 size={17} />}
      >
        {notes.announcements.length ? (
          notes.announcements.map((item, index) => (
            <div className="announcement" key={index}>
              <span>{item.text}</span>
              {item.deadline && <b>{item.deadline}</b>}
            </div>
          ))
        ) : (
          <p className="empty-note">No announcements were identified.</p>
        )}
      </NoteSection>
      <NoteSection
        title="Off-topic remarks"
        tone="green"
        icon={<MessageCircle size={17} />}
      >
        {notes.off_topic.length ? (
          notes.off_topic.map((item, index) => (
            <p className="off-topic" key={index}>
              {item.summary}
            </p>
          ))
        ) : (
          <p className="empty-note">The class stayed on topic.</p>
        )}
      </NoteSection>
    </article>
  );
}

function NoteSection({
  title,
  tone,
  icon,
  children,
}: {
  title: string;
  tone: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className={`note-section ${tone}`}>
      <div className="note-section-title">
        <span>{icon}</span>
        <h2>{title}</h2>
      </div>
      {children}
    </section>
  );
}

function ChatView({
  lectureId,
  userInitial,
  sessions,
  active,
  messages,
  onCreate,
  onChoose,
  setMessages,
  onSeek,
}: {
  lectureId: string;
  userInitial: string;
  sessions: ChatSession[];
  active: ChatSession | null;
  messages: ChatMessage[];
  onCreate: () => void;
  onChoose: (session: ChatSession, loadMessages?: boolean) => void;
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
  onSeek: (startSeconds: number) => void;
}) {
  const [question, setQuestion] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const hasPendingReply = messages.some(
    (message) => message.role === "assistant" && message.status === "pending",
  );
  useEffect(() => {
    if (!active || !hasPendingReply) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const rows = await api<ChatMessage[]>(
          `/chat-sessions/${active.id}/messages`,
        );
        if (cancelled) return;
        setMessages(rows);
        if (
          rows.some(
            (message) =>
              message.role === "assistant" && message.status === "pending",
          )
        ) {
          timer = window.setTimeout(poll, 1800);
        }
      } catch (reason) {
        if (cancelled) return;
        setError(userFacingError(reason));
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
    setQuestion("");
    setMessages((current) => [...current, optimistic, optimisticReply]);
    try {
      if (!chat) {
        chat = await api<ChatSession>(`/lectures/${lectureId}/chat-sessions`, {
          method: "POST",
        });
        onChoose(chat, false);
      }
      const turn = await api<{
        user_message: ChatMessage;
        assistant_message: ChatMessage;
      }>(`/lectures/${lectureId}/chat`, {
        method: "POST",
        body: JSON.stringify({ session_id: chat.id, message: text }),
      });
      setMessages((current) => [
        ...current.filter(
          (message) =>
            message.id !== optimistic.id && message.id !== optimisticReply.id,
        ),
        turn.user_message,
        turn.assistant_message,
      ]);
      onChoose({ ...chat, title: text.slice(0, 80) }, false);
    } catch (reason) {
      setQuestion((current) => current || text);
      setMessages((current) =>
        current.map((message) =>
          message.id === optimisticReply.id
            ? {
                ...message,
                status: "error",
                content: "The question could not be sent. Please try again.",
              }
            : message,
        ),
      );
      setError(userFacingError(reason));
    } finally {
      setSending(false);
    }
  }
  return (
    <div className="chat-panel">
      <div className="chat-panel-head">
        <div>
          <span className="eyebrow">LECTURE ASSISTANT</span>
          <h2>{active?.title || "Ask anything from class"}</h2>
        </div>
        <div className="chat-head-actions">
          <button
            className="button button-outline compact"
            onClick={() => setShowHistory(!showHistory)}
          >
            <Clock3 size={15} /> History <ChevronDown size={14} />
          </button>
          <button className="button button-outline compact" onClick={onCreate}>
            <Plus size={15} /> New chat
          </button>
        </div>
      </div>
      {showHistory && (
        <div className="chat-history-dropdown">
          {sessions.map((chat) => (
            <button
              key={chat.id}
              onClick={() => {
                onChoose(chat);
                setShowHistory(false);
              }}
            >
              <MessageCircle size={15} />
              {chat.title}
            </button>
          ))}
        </div>
      )}
      <div className="chat-messages">
        {!messages.length && (
          <div className="chat-welcome">
            <span>
              <Sparkles size={20} />
            </span>
            <h3>Your lecture, on demand.</h3>
            <p>
              Ask a question and I’ll point you to the relevant moment in the
              recording.
            </p>
            <div className="prompt-suggestions">
              {[
                "What should I focus on for the exam?",
                "Explain the main idea simply",
                "Where does the lecture discuss its key example?",
              ].map((text) => (
                <button key={text} onClick={() => setQuestion(text)}>
                  {text} <ArrowRight size={14} />
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((message) => {
          const sources = displaySources(message);
          return (
            <div key={message.id} className={`chat-message ${message.role}`}>
              <span className="message-avatar">
                {message.role === "assistant" ? (
                  <Sparkles size={15} />
                ) : (
                  userInitial
                )}
              </span>
              <div
                className={`message-content ${message.status === "error" ? "message-failed" : ""}`}
              >
                {message.status === "pending" ? (
                  <div className="thinking">
                    <span />
                    <span />
                    <span />
                  </div>
                ) : message.role === "assistant" ? (
                  <ReactMarkdown>{message.content}</ReactMarkdown>
                ) : (
                  <p>{message.content}</p>
                )}
                {message.role === "assistant" &&
                  message.intent === "off_topic" &&
                  message.grounded === false && (
                    <p className="not-covered-note">
                      Not covered in this lecture
                    </p>
                  )}
                {sources.length > 0 && (
                  <div className="source-list">
                    <span>
                      {message.intent === "locate"
                        ? `MENTIONED AT · ${sources.length} passages`
                        : "RELATED MOMENTS"}
                    </span>
                    {sources.map((source) => {
                      const label = sourceLabel(source);
                      return (
                        <button
                          type="button"
                          className="source-chip"
                          key={`${source.start}-${source.end ?? source.start}`}
                          title={source.text_snippet || ""}
                          aria-label={`Jump to ${label}`}
                          onClick={() => onSeek(source.start ?? 0)}
                        >
                          <Clock3 size={13} /> {label}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {error && <div className="chat-error">{error}</div>}
      <form className="chat-composer" onSubmit={submit}>
        <textarea
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder="Ask a question about this lecture…"
          rows={1}
        />
        <span>
          {hasPendingReply
            ? "Waiting for the previous answer…"
            : "Answers are grounded in your lecture"}
        </span>
        <button
          disabled={sending || hasPendingReply || !question.trim()}
          aria-label="Send question"
        >
          {sending ? (
            <LoaderCircle className="spin" size={17} />
          ) : (
            <Send size={17} />
          )}
        </button>
      </form>
    </div>
  );
}

function UploadModal({
  onClose,
  resumeLecture,
  onCreated,
  onUploadProgress,
  onUploadComplete,
  onUploadFailed,
}: {
  onClose: () => void;
  resumeLecture?: Lecture;
  onCreated: (lecture: Lecture, uploadPending: boolean) => void;
  onUploadProgress: (lectureId: string, percentage: number | null) => void;
  onUploadComplete: (lecture: Lecture) => void;
  onUploadFailed: (lectureId: string, message: string) => void;
}) {
  const [mode, setMode] = useState<"audio" | "transcript">("audio");
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [subject, setSubject] = useState(resumeLecture?.subject || "");
  const [transcript, setTranscript] = useState("");
  const [language, setLanguage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [drag, setDrag] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (mode === "audio" && !file) {
      setError("Choose an audio file to continue.");
      return;
    }
    if (mode === "transcript" && !transcript.trim()) {
      setError("Paste a transcript to continue.");
      return;
    }
    if (mode === "audio" && file) {
      const extension = file.name.match(/\.[^.]+$/)?.[0].toLowerCase() || "";
      if (
        ![
          ".mp3",
          ".wav",
          ".m4a",
          ".mp4",
          ".aac",
          ".ogg",
          ".flac",
          ".webm",
          ".opus",
          ".wma",
        ].includes(extension)
      ) {
        setError(
          "Unsupported audio type. Use MP3, WAV, M4A, MP4, AAC, OGG, FLAC, WEBM, OPUS, or WMA.",
        );
        return;
      }
      if (file.size > 500 * 1024 * 1024) {
        setError("Audio files must be 500 MB or smaller.");
        return;
      }
    }
    const form = new FormData();
    form.append("title", title);
    form.append("subject", subject);
    setBusy(true);
    setError("");
    let lecture: Lecture | null = null;
    let pendingAudioKey: string | null = null;
    try {
      if (mode === "audio" && file) {
        if (!supabase) throw new Error(supabaseConfigError);
        const { data, error: sessionError } = await supabase.auth.getSession();
        if (sessionError) throw sessionError;
        if (!data.session?.access_token || !data.session.user.id) {
          throw new Error("Your session expired. Please sign in again.");
        }
        const pendingUpload = getAudioObjectPath(data.session.user.id, file);
        pendingAudioKey = pendingUpload.key;
        const fallbackKey = `${pendingUpload.key}:direct-fallback`;
        const objectPath = resumeLecture?.audio_path || pendingUpload.path;
        if (
          resumeLecture &&
          !objectPath.startsWith(`${data.session.user.id}/`)
        ) {
          throw new Error(
            "This upload belongs to a different account. Sign in to the account that started it.",
          );
        }
        localStorage.setItem(pendingUpload.key, objectPath);
        if (!resumeLecture) {
          form.append("storage_path", objectPath);
          form.append("source_filename", file.name);
          form.append("language", language);
          form.append("defer_processing", "true");
          lecture = await api<Lecture>("/lectures", {
            method: "POST",
            body: form,
          });
          onCreated(lecture, true);
        } else {
          lecture = resumeLecture;
        }
        onUploadProgress(lecture.id, 0);
        onClose();
        await uploadAudioToStorage(
          file,
          objectPath,
          data.session.access_token,
          fallbackKey,
          (percentage) => onUploadProgress(lecture!.id, percentage),
        );
        const started = await api<Lecture>(
          `/lectures/${lecture.id}/start-upload`,
          { method: "POST" },
        );
        localStorage.removeItem(pendingAudioKey);
        localStorage.removeItem(`${pendingAudioKey}:direct-fallback`);
        onUploadComplete(started);
        return;
      } else {
        form.append("transcript_text", transcript);
      }
      lecture = await api<Lecture>("/lectures", { method: "POST", body: form });
      onCreated(lecture, false);
      onClose();
    } catch (reason) {
      if (lecture) {
        onUploadFailed(lecture.id, userFacingError(reason));
        return;
      }
      setError(userFacingError(reason));
      setBusy(false);
    }
  }
  return (
    <motion.div
      className="modal-backdrop"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onMouseDown={(event) => {
        if (!busy && event.target === event.currentTarget) onClose();
      }}
    >
      <motion.section
        className="upload-modal"
        initial={{ opacity: 0, y: 18, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 12 }}
        transition={{ duration: 0.2 }}
      >
        <div className="modal-head">
          <div>
            <span className="eyebrow">
              {resumeLecture
                ? "CONTINUE YOUR UPLOAD"
                : "A NEW CLASS, MADE CLEAR"}
            </span>
            <h2>{resumeLecture ? "Resume audio upload" : "Add a lecture"}</h2>
            <p>
              {resumeLecture
                ? "Choose the same recording to resume its secure upload."
                : "Upload an audio recording or bring in a transcript."}
            </p>
          </div>
          <button className="icon-btn" disabled={busy} onClick={onClose}>
            <X size={19} />
          </button>
        </div>
        {!resumeLecture && (
          <div className="upload-tabs">
            <button
              type="button"
              className={mode === "audio" ? "active" : ""}
              onClick={() => setMode("audio")}
            >
              <FileAudio2 size={16} /> Audio recording
            </button>
            <button
              type="button"
              className={mode === "transcript" ? "active" : ""}
              onClick={() => setMode("transcript")}
            >
              <FileText size={16} /> Paste transcript
            </button>
          </div>
        )}
        <form onSubmit={submit}>
          {mode === "audio" ? (
            <label
              className={`drop-zone ${drag ? "dragging" : ""}`}
              onDragOver={(event) => {
                event.preventDefault();
                setDrag(true);
              }}
              onDragLeave={() => setDrag(false)}
              onDrop={(event) => {
                event.preventDefault();
                setDrag(false);
                setFile(event.dataTransfer.files[0] || null);
              }}
            >
              <input
                type="file"
                accept=".mp3,.wav,.m4a,.mp4,.aac,.ogg,.flac,.webm,.opus,.wma,audio/*"
                onChange={(event) => setFile(event.target.files?.[0] || null)}
              />
              <span className="upload-icon">
                <UploadCloud size={21} />
              </span>
              <b>{file ? file.name : "Drop your audio here"}</b>
              <span>
                {file
                  ? `${(file.size / (1024 * 1024)).toFixed(1)} MB · Ready to upload`
                  : "or click to browse · MP3, WAV, M4A and more"}
              </span>
            </label>
          ) : (
            <label className="transcript-field">
              <span>Paste your lecture transcript</span>
              <textarea
                rows={7}
                value={transcript}
                onChange={(event) => setTranscript(event.target.value)}
                placeholder="Paste your transcript here…"
              />
            </label>
          )}
          {!resumeLecture && (
            <div className="upload-fields">
              <label>
                Title <small>Optional</small>
                <input
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  placeholder="We’ll create one from your lecture"
                />
              </label>
              <label>
                Subject folder <small>Optional</small>
                <input
                  value={subject}
                  onChange={(event) => setSubject(event.target.value)}
                  placeholder="e.g. Operating Systems"
                />
              </label>
            </div>
          )}
          {mode === "audio" && !resumeLecture && (
            <label className="language-select">
              Audio language
              <select
                value={language}
                onChange={(event) => setLanguage(event.target.value)}
              >
                <option value="">Auto-detect</option>
                <option value="en">English</option>
                <option value="hi">Hindi</option>
                <option value="gu">Gujarati</option>
              </select>
            </label>
          )}
          {error && <div className="form-alert">{error}</div>}
          <div className="modal-foot">
            <span>
              <span className="secure-dot" /> Private to your account
            </span>
            <button className="button button-dark" disabled={busy}>
              {busy ? (
                <LoaderCircle className="spin" size={17} />
              ) : (
                <Sparkles size={16} />
              )}
              {busy
                ? "Creating upload…"
                : resumeLecture
                  ? "Resume upload"
                  : "Create my notes"}{" "}
              <ArrowRight size={16} />
            </button>
          </div>
        </form>
      </motion.section>
    </motion.div>
  );
}

export default App;
