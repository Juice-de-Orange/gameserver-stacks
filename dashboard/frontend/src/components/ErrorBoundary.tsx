import { Component, ErrorInfo, ReactNode } from "react";
import i18n from "../i18n";
import { Button, GhostButton } from "./ui";

interface State {
  error: Error | null;
}

/** Catalog text with a hard English fallback: this panel has to render even if the translation
 *  layer itself is what failed (not initialised, catalog missing, `t` throwing). */
function text(key: string, fallback: string): string {
  try {
    const value = i18n.t(`errorBoundary:${key}`, { defaultValue: fallback });
    return typeof value === "string" && value ? value : fallback;
  } catch {
    return fallback;
  }
}

/** Catches render errors of a view → a themed panel instead of a white screen.
 *  `key={pathname}` in Shell resets it on navigation. */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }
  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("UI error:", error, info);
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div className="grid min-h-[60vh] place-items-center p-6">
        <div className="max-w-md space-y-3 rounded-2xl border border-danger-border bg-surface-card p-6 text-center shadow-panel">
          <div className="text-stencil text-xl text-danger-text">{text("title", "Something went wrong")}</div>
          <p className="text-sm text-fg-muted">
            {text("body", "This view has crashed. Try again or reload the page.")}
          </p>
          <pre className="overflow-auto rounded bg-surface-raised p-2 text-left text-xs text-fg-dim">
            {String(this.state.error.message || this.state.error)}
          </pre>
          <div className="flex justify-center gap-2">
            <GhostButton onClick={() => this.setState({ error: null })}>{text("retry", "Retry")}</GhostButton>
            <Button onClick={() => location.reload()}>{text("reload", "Reload")}</Button>
          </div>
        </div>
      </div>
    );
  }
}
