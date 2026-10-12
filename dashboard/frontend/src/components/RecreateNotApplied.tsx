// A failed recreate is rolled back to the previous container, but the settings that triggered it
// were saved before the swap (edit page: save, then recreate). The edit page would then show a RAM
// limit or a port the running container does not have. This says so, from the version history:
// the newest entry is a recreate that did not complete. Any later successful swap (a recreate or
// a version change) builds the container from the saved values, and the note disappears.
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiJson } from "../api";
import { Alert } from "./ui";

interface HistoryRow {
  id: number;
  direction: string;
  status: string;
}

const NOT_APPLIED = new Set(["rolled_back", "rolled_back_partial", "failed"]);

/** Is the newest entry of the version history a recreate whose settings did not take effect? */
export function lastRecreateNotApplied(rows: HistoryRow[]): boolean {
  const newest = rows[0];
  return !!newest && newest.direction === "recreate" && NOT_APPLIED.has(newest.status);
}

export function RecreateNotApplied({ serverKey }: { serverKey: string }) {
  const { t } = useTranslation("recreateNotApplied");
  const [show, setShow] = useState(false);

  useEffect(() => {
    let alive = true;
    apiJson<HistoryRow[]>(`/servers/${serverKey}/version/history`)
      .then((rows) => alive && setShow(lastRecreateNotApplied(rows)))
      // No history (no permission, request failed): no claim either way.
      .catch(() => alive && setShow(false));
    return () => {
      alive = false;
    };
  }, [serverKey]);

  if (!show) return null;
  return <Alert kind="warn">{t("text")}</Alert>;
}
