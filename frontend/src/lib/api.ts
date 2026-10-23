import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(code);
  }
}
const STATUS_CODES: Record<number, string> = {
  401: "unauthorized",
  403: "forbidden",
  404: "notFound",
  408: "timeout",
  429: "rateLimited",
  504: "timeout",
};
// Transient failures are worth one automatic retry; permission and missing-resource errors are not.
export const isTransient = (error: ApiError) =>
  error.status === 0 || error.status === 429 || error.status >= 500;
export async function request<T>(
  path: string,
  language: string,
  signal?: AbortSignal,
  body?: unknown,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      signal,
      headers: {
        "Accept-Language": language,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      ...(body ? { method: "POST", body: JSON.stringify(body) } : {}),
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError(0, navigator.onLine === false ? "offline" : "network");
  }
  if (!response.ok)
    throw new ApiError(
      response.status,
      STATUS_CODES[response.status] ?? "loadError",
    );
  if (!response.headers.get("content-type")?.includes("application/json"))
    throw new ApiError(502, "loadError");
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(502, "loadError");
  }
}
/** The selected workspace. One source: the `workspace_id` query parameter. */
export function useScope() {
  const [params] = useSearchParams();
  return params.get("workspace_id") ?? "";
}
/** `path` within `scope`, unless the path already names its own workspace. */
export function withScope(path: string, scope: string) {
  const [base, query] = path.split("?");
  const params = new URLSearchParams(query);
  if (scope && !params.has("workspace_id")) params.set("workspace_id", scope);
  return `${base}${params.size ? `?${params}` : ""}`;
}
export function useApi<T>(
  path: string,
  options: { scoped?: boolean; enabled?: boolean } = {},
) {
  const { i18n } = useTranslation();
  const scope = useScope();
  const full = options.scoped === false ? path : withScope(path, scope);
  return useQuery<T, ApiError>({
    queryKey: [full, i18n.language],
    queryFn: ({ signal }) => request<T>(full, i18n.language, signal),
    staleTime: 60_000,
    retry: (count, error) => count < 1 && isTransient(error),
    enabled: options.enabled ?? true,
  });
}
