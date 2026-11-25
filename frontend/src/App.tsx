import { useEffect, useMemo, useState, type FormEvent } from "react";
import {
  ApiError,
  answerTask,
  fetchAllRepos,
  fetchTask,
  fetchTasks,
  loginWithGithub,
  logout,
  startTaskForRepo,
  taskStreamUrl,
  type Repo,
  type TaskEvent,
  type TaskListItem,
} from "./api";

const EVENT_TYPES = [
  "reasoning",
  "tool_call",
  "tool_result",
  "user_prompt",
  "user_answer",
  "done",
];

function taskIdFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get("task");
}

function setTaskInUrl(taskId: string | null) {
  const url = new URL(window.location.href);
  if (taskId) {
    url.searchParams.set("task", taskId);
  } else {
    url.searchParams.delete("task");
  }
  window.history.replaceState({}, "", url);
}

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

function truncate(text: string, max = 72): string {
  const trimmed = text.trim().replace(/\s+/g, " ");
  return trimmed.length > max ? `${trimmed.slice(0, max)}…` : trimmed;
}

function unansweredQuestion(events: TaskEvent[]): string | null {
  let question: string | null = null;
  for (const event of events) {
    if (event.event_type === "user_prompt" && typeof event.payload.question === "string") {
      question = event.payload.question;
    }
    if (event.event_type === "user_answer" || event.event_type === "done") {
      question = null;
    }
  }
  return question;
}

function formatEvent(event: TaskEvent): string {
  const payload = event.payload;
  if (typeof payload.text === "string") return payload.text;
  if (typeof payload.question === "string") return payload.question;
  if (typeof payload.answer === "string") return payload.answer;
  if (typeof payload.tool === "string") {
    return `${payload.tool} ${JSON.stringify(payload.args ?? payload.result ?? "")}`;
  }
  if (typeof payload.status === "string") {
    return payload.reason ? `${payload.status} (${String(payload.reason)})` : payload.status;
  }
  return JSON.stringify(payload);
}

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [repos, setRepos] = useState<Repo[] | null>(null);
  const [reposExpanded, setReposExpanded] = useState(false);
  const [reposLoading, setReposLoading] = useState(false);
  const [repoError, setRepoError] = useState<string | null>(null);
  const [selectedRepo, setSelectedRepo] = useState<Repo | null>(null);
  const [tasks, setTasks] = useState<TaskListItem[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [tasksError, setTasksError] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(taskIdFromUrl);
  const [activeStatus, setActiveStatus] = useState<string | null>(null);
  const [activeDescription, setActiveDescription] = useState<string | null>(null);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [question, setQuestion] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [answering, setAnswering] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("error")) {
      setError("GitHub login failed. Try again.");
      params.delete("error");
      const url = new URL(window.location.href);
      url.search = params.toString();
      window.history.replaceState({}, "", url);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setReposLoading(true);
    setRepoError(null);
    fetchAllRepos()
      .then((result) => {
        if (cancelled) return;
        if (result === null) {
          setAuthed(false);
          setRepos(null);
        } else {
          setAuthed(true);
          setRepos(result);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setRepoError(err instanceof ApiError ? err.message : "Could not load repositories.");
        setAuthed((current) => (current === true ? true : false));
      })
      .finally(() => {
        if (!cancelled) setReposLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function refreshTasks() {
    setTasksLoading(true);
    setTasksError(null);
    try {
      const result = await fetchTasks();
      if (result === null) {
        setAuthed(false);
        setTasks([]);
        return;
      }
      setTasks(result);
    } catch (err) {
      setTasksError(err instanceof ApiError ? err.message : "Could not load tasks.");
    } finally {
      setTasksLoading(false);
    }
  }

  useEffect(() => {
    if (authed) {
      void refreshTasks();
    }
  }, [authed]);

  useEffect(() => {
    if (!authed || !activeTaskId) return;
    let cancelled = false;
    setStreamError(null);
    fetchTask(activeTaskId)
      .then((snapshot) => {
        if (cancelled) return;
        const loaded = snapshot.events ?? [];
        setEvents(loaded);
        setActiveStatus(snapshot.status);
        setActiveDescription(snapshot.description);
        setQuestion(snapshot.status === "waiting_for_user" ? unansweredQuestion(loaded) : null);
      })
      .catch((err) => {
        if (!cancelled) {
          setStreamError(err instanceof ApiError ? err.message : "Could not load task.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [authed, activeTaskId]);

  useEffect(() => {
    if (!authed || !activeTaskId) return;
    const source = new EventSource(taskStreamUrl(activeTaskId), { withCredentials: true });
    const seen = new Set<string>();
    const onEvent = (type: string) => (message: MessageEvent) => {
      const key = `${message.lastEventId}:${type}:${message.data}`;
      if (seen.has(key)) return;
      seen.add(key);
      let payload: Record<string, unknown> = {};
      try {
        payload = JSON.parse(message.data) as Record<string, unknown>;
      } catch {
        payload = { raw: message.data };
      }
      const id = Number(message.lastEventId) || Date.now();
      const event: TaskEvent = {
        id,
        event_type: type,
        payload,
        timestamp: null,
      };
      setEvents((current) => {
        if (current.some((item) => item.id === event.id && item.event_type === type)) {
          return current;
        }
        return [...current, event];
      });
      if (type === "user_prompt" && typeof payload.question === "string") {
        setQuestion(payload.question);
        setActiveStatus("waiting_for_user");
      }
      if (type === "user_answer") {
        setQuestion(null);
        setActiveStatus("running");
      }
      if (type === "done") {
        setQuestion(null);
        setActiveStatus(typeof payload.status === "string" ? payload.status : "done");
        void refreshTasks();
      }
    };
    for (const type of EVENT_TYPES) {
      source.addEventListener(type, onEvent(type));
    }
    source.onerror = () => {
      /* EventSource reconnects and sends Last-Event-ID */
    };
    return () => source.close();
  }, [authed, activeTaskId]);

  const selectedRepoName = selectedRepo?.full_name ?? null;

  const composerHint = useMemo(() => {
    if (!selectedRepoName) return "Select a repository in the sidebar first.";
    return `Task will run against ${selectedRepoName}.`;
  }, [selectedRepoName]);

  async function onLogout() {
    await logout();
    setAuthed(false);
    setRepos(null);
    setTasks([]);
    setSelectedRepo(null);
    setActiveTaskId(null);
    setEvents([]);
    setQuestion(null);
    setTaskInUrl(null);
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const text = description.trim();
    if (!selectedRepo || !text || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await startTaskForRepo(selectedRepo.full_name, text);
      setDescription("");
      setActiveTaskId(created.task_id);
      setActiveStatus("running");
      setActiveDescription(text);
      setEvents([]);
      setQuestion(null);
      setTaskInUrl(created.task_id);
      await refreshTasks();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Please re-authenticate with GitHub.");
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Could not start task.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function onAnswer(event: FormEvent) {
    event.preventDefault();
    if (!activeTaskId || !answer.trim()) return;
    setAnswering(true);
    setError(null);
    try {
      await answerTask(activeTaskId, answer.trim());
      setAnswer("");
      setQuestion(null);
      setActiveStatus("running");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not submit answer.");
    } finally {
      setAnswering(false);
    }
  }

  function openTask(task: TaskListItem) {
    setActiveTaskId(task.task_id);
    setActiveStatus(task.status);
    setActiveDescription(task.description);
    setEvents([]);
    setQuestion(null);
    setStreamError(null);
    setTaskInUrl(task.task_id);
  }

  if (authed !== true) {
    return (
      <div className="login">
        <h1>Devin</h1>
        {error ? <p className="error">{error}</p> : null}
        {authed === null ? (
          <p>Loading…</p>
        ) : (
          <>
            <p>Sign in with GitHub to pick a repository and run a task.</p>
            <button type="button" className="primary" onClick={loginWithGithub}>
              Login with GitHub
            </button>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <header>
          <h1>Devin</h1>
          {authed ? (
            <button type="button" onClick={onLogout}>
              Log out
            </button>
          ) : null}
        </header>

        <section>
          <h2>Repositories</h2>
          {reposLoading ? <p>Loading repositories…</p> : null}
          {repoError ? <p className="error">{repoError}</p> : null}
          {!reposLoading && !repoError && repos && repos.length === 0 ? (
            <p>No repositories yet.</p>
          ) : null}
          {repos && repos.length > 0 ? (
            <>
              <ul className={`repo-list${reposExpanded ? " expanded" : ""}`}>
                {(reposExpanded ? repos : repos.slice(0, 5)).map((repo) => (
                  <li key={repo.id}>
                    <button
                      type="button"
                      className={repo.full_name === selectedRepoName ? "selected" : ""}
                      onClick={() => setSelectedRepo(repo)}
                    >
                      <span>{repo.full_name}</span>
                      {repo.private ? <span className="badge">private</span> : null}
                    </button>
                  </li>
                ))}
              </ul>
              {repos.length > 5 ? (
                <button
                  type="button"
                  className="repo-more"
                  onClick={() => setReposExpanded((open) => !open)}
                >
                  {reposExpanded ? "Show less" : `More repos (${repos.length - 5})`}
                </button>
              ) : null}
            </>
          ) : null}
        </section>

        <section>
          <h2>Tasks</h2>
          {tasksLoading ? <p>Loading tasks…</p> : null}
          {tasksError ? <p className="error">{tasksError}</p> : null}
          {!tasksLoading && !tasksError && tasks.length === 0 ? <p>No tasks yet.</p> : null}
          <ul className="task-list">
            {tasks.map((task) => (
              <li key={task.task_id}>
                <button
                  type="button"
                  className={task.task_id === activeTaskId ? "selected" : ""}
                  onClick={() => openTask(task)}
                >
                  <span className="task-desc">{truncate(task.description)}</span>
                  <span className={`status status-${task.status}`}>{task.status}</span>
                  <span className="when">{relativeTime(task.created_at)}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      </aside>

      <main>
        {error ? <p className="error">{error}</p> : null}

        {activeTaskId ? (
          <section className="stream">
            <header className="stream-head">
              <div>
                <h2>{activeDescription ? truncate(activeDescription, 120) : "Task"}</h2>
                <p>
                  {activeStatus ?? "loading"}
                  {selectedRepoName ? ` · ${selectedRepoName}` : null}
                </p>
              </div>
            </header>
            {streamError ? <p className="error">{streamError}</p> : null}
            {events.length === 0 && !streamError ? <p>Waiting for agent output…</p> : null}
            <ol className="events">
              {events.map((event) => (
                <li key={`${event.event_type}-${event.id}`} className={`event event-${event.event_type}`}>
                  <strong>{event.event_type}</strong>
                  <span>{formatEvent(event)}</span>
                </li>
              ))}
            </ol>
            {question ? (
              <form className="ask-user" onSubmit={onAnswer}>
                <p>{question}</p>
                <textarea
                  value={answer}
                  onChange={(change) => setAnswer(change.target.value)}
                  rows={2}
                  placeholder="Your answer"
                />
                <button type="submit" className="primary" disabled={answering || !answer.trim()}>
                  {answering ? "Sending…" : "Submit answer"}
                </button>
              </form>
            ) : null}
          </section>
        ) : (
          <section className="empty">
            <h2>New task</h2>
            <p>Select a repo, describe the work, and watch the agent stream here.</p>
          </section>
        )}

        <form className="composer" onSubmit={onSubmit}>
          <label htmlFor="task-input">Describe what you want the agent to do.</label>
          <textarea
            id="task-input"
            value={description}
            onChange={(change) => setDescription(change.target.value)}
            rows={3}
            placeholder="Describe what you want the agent to do."
            disabled={submitting}
          />
          <div className="composer-row">
            <p>{composerHint}</p>
            <button
              type="submit"
              className="primary"
              disabled={submitting || !selectedRepo || !description.trim()}
            >
              {submitting ? "Starting…" : "Run task"}
            </button>
          </div>
        </form>
      </main>
    </div>
  );
}
