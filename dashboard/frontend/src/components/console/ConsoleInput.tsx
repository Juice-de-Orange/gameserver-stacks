import { FormEvent, KeyboardEvent, MutableRefObject, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button, Input } from "../ui";

/**
 * Input line with command history (↑/↓). Uses the shared history ref of the live console.
 * `disabled` e.g. when the user has no console permission. Without a `placeholder` the default
 * text is used, in the active language.
 */
export function ConsoleInput({
  onSend,
  disabled = false,
  history,
  placeholder,
}: {
  onSend: (cmd: string) => void;
  disabled?: boolean;
  history: MutableRefObject<string[]>;
  placeholder?: string;
}) {
  const { t } = useTranslation("consoleInput");
  const [cmd, setCmd] = useState("");
  const histIdx = useRef(-1);

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!cmd.trim()) return;
    onSend(cmd);
    histIdx.current = -1;
    setCmd("");
  }
  function onKey(e: KeyboardEvent) {
    if (e.key === "ArrowUp") {
      e.preventDefault();
      const next = Math.min(histIdx.current + 1, history.current.length - 1);
      if (next >= 0) {
        histIdx.current = next;
        setCmd(history.current[next]);
      }
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      const next = Math.max(histIdx.current - 1, -1);
      histIdx.current = next;
      setCmd(next >= 0 ? history.current[next] : "");
    }
  }

  return (
    <form onSubmit={submit} className="flex gap-2">
      <Input
        value={cmd}
        onChange={(e) => setCmd(e.target.value)}
        onKeyDown={onKey}
        disabled={disabled}
        placeholder={disabled ? t("noPermission") : (placeholder ?? t("placeholder"))}
        className="font-mono"
      />
      <Button type="submit" disabled={disabled || !cmd.trim()}>
        {t("send")}
      </Button>
    </form>
  );
}
