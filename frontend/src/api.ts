const API = import.meta.env.VITE_API_URL ?? "";

export type Repo = {
  id: number;
  full_name: string;
  private: boolean;
  html_url: string;
  description: string | null;
  default_branch: string | null;
  updated_at: string | null;
};

export type RepoPage = {
  repos: Repo[];
  page: number;
  per_page: number;
  has_next: boolean;
  has_prev: boolean;
  next_page: number | null;
  prev_page: number | null;
};

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request(path: string, init: RequestInit = {}, retried = false): Promise<Response> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    credentials: "include",
  });
  if (response.status === 401 && !retried && path !== "/auth/refresh") {
    const refreshed = await fetch(`${API}/auth/refresh`, {
      method: "POST",
      credentials: "include",
    });
    if (refreshed.ok) {
      return request(path, init, true);
    }
  }
  return response;
}

async function readDetail(response: Response, fallback: string): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) {
      return body.detail;
    }
  } catch {
    /* not json */
  }
  return fallback;
}

export function loginWithGithub() {
  window.location.href = `${API}/auth/github/login`;
}

export async function logout(): Promise<void> {
  await request("/auth/logout", { method: "POST" });
}

export async function fetchRepos(page: number): Promise<RepoPage | null> {
  const response = await request(`/repos?page=${page}&per_page=100`);
  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw new ApiError(
      response.status,
      await readDetail(response, `Failed to load repositories (${response.status})`),
    );
  }
  return response.json() as Promise<RepoPage>;
}

export async function fetchAllRepos(): Promise<Repo[] | null> {
  const first = await fetchRepos(1);
  if (first === null) {
    return null;
  }
  const repos = [...first.repos];
  let page = first.next_page;
  while (page) {
    const next = await fetchRepos(page);
    if (next === null) {
      break;
    }
    repos.push(...next.repos);
    page = next.next_page;
  }
  return repos;
}

export async function createSandbox(repoFullName: string): Promise<{ sandbox_id: string }> {
  const response = await request("/sandboxes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo_full_name: repoFullName }),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response, "Failed to create sandbox"));
  }
  return response.json() as Promise<{ sandbox_id: string }>;
}

export type TaskEvent = {
  id: number;
  event_type: string;
  payload: Record<string, unknown>;
  timestamp: string | null;
};

export type TaskSnapshot = {
  task_id: string;
  sandbox_id: string;
  description: string;
  status: string;
  created_at: string | null;
  completed_at: string | null;
  repo_url?: string | null;
  events?: TaskEvent[];
};

export type TaskListItem = {
  task_id: string;
  sandbox_id: string;
  repo_url: string | null;
  description: string;
  status: string;
  created_at: string | null;
  completed_at: string | null;
};

export async function fetchTasks(): Promise<TaskListItem[] | null> {
  const response = await request("/tasks");
  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw new ApiError(
      response.status,
      await readDetail(response, `Failed to load tasks (${response.status})`),
    );
  }
  const body = (await response.json()) as { tasks: TaskListItem[] };
  return body.tasks;
}

export async function createTask(
  sandboxId: string,
  description: string,
): Promise<{ task_id: string }> {
  const response = await request("/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sandbox_id: sandboxId, description }),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response, "Failed to start task"));
  }
  return response.json() as Promise<{ task_id: string }>;
}

export async function startTaskForRepo(
  repoFullName: string,
  description: string,
): Promise<{ task_id: string }> {
  const sandbox = await createSandbox(repoFullName);
  return createTask(sandbox.sandbox_id, description);
}

export async function fetchTask(taskId: string): Promise<TaskSnapshot> {
  const response = await request(`/tasks/${taskId}`);
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response, "Failed to load task"));
  }
  return response.json() as Promise<TaskSnapshot>;
}

export async function answerTask(taskId: string, answer: string): Promise<void> {
  const response = await request(`/tasks/${taskId}/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answer }),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await readDetail(response, "Failed to submit answer"));
  }
}

export function taskStreamUrl(taskId: string): string {
  return `${API}/tasks/${taskId}/stream`;
}
