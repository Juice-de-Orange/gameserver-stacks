import { FormEvent, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Bug,
  Check,
  ExternalLink,
  HelpCircle,
  MessageSquare,
  Package,
  Plus,
  Send,
  ShieldAlert,
  Ticket as TicketIcon,
  type LucideIcon,
} from "lucide-react";
import { api, apiJson } from "../api";
import { useAuth } from "../auth";
import { useOverview } from "../components/overview";
import { useActiveServer } from "../components/ServerContext";
import { safeUrl } from "../lib/url";
import { useToast } from "../components/feedback";
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  GhostButton,
  Input,
  SectionTitle,
  Select,
  Textarea,
  cx,
} from "../components/ui";
import { fmtRelative } from "../lib/format";

// `label`, `titlePlaceholder` and `body` are catalog keys: translated when used, not at import.
interface Template {
  key: string;
  label: string;
  icon: LucideIcon;
  showWorkshop?: boolean;
  titlePlaceholder: string;
  /** No key = the template starts with an empty description. */
  body?: string;
}

const TEMPLATES: Template[] = [
  {
    key: "mod_request",
    label: "template.modRequest.label",
    icon: Package,
    showWorkshop: true,
    titlePlaceholder: "template.modRequest.title",
    body: "template.modRequest.body",
  },
  {
    key: "bug_report",
    label: "template.bugReport.label",
    icon: Bug,
    titlePlaceholder: "template.bugReport.title",
    body: "template.bugReport.body",
  },
  {
    key: "player_report",
    label: "template.playerReport.label",
    icon: ShieldAlert,
    titlePlaceholder: "template.playerReport.title",
    body: "template.playerReport.body",
  },
  { key: "question", label: "template.question.label", icon: HelpCircle, titlePlaceholder: "template.question.title" },
  { key: "general", label: "template.general.label", icon: MessageSquare, titlePlaceholder: "template.general.title" },
];

// Catalog keys per category; an unknown category is shown as it came.
const CAT_LABEL: Record<string, string> = {
  mod_request: "category.modRequest",
  bug_report: "category.bugReport",
  player_report: "category.playerReport",
  question: "category.question",
  general: "category.general",
};

const STATUS: { key: string; label: string; tone: any }[] = [
  { key: "open", label: "status.open", tone: "info" },
  { key: "in_review", label: "status.inReview", tone: "warn" },
  { key: "approved", label: "status.approved", tone: "toxic" },
  { key: "rejected", label: "status.rejected", tone: "blood" },
  { key: "done", label: "status.done", tone: "info" },
];
const statusOf = (k: string) => STATUS.find((s) => s.key === k) || STATUS[0];

function parseWorkshopId(url: string): string {
  const m = url.match(/[?&]id=(\d+)/);
  return m ? m[1] : "";
}

export default function Tickets() {
  // `tr`, not `t`: this page already uses `t` for the selected ticket.
  const { t: tr } = useTranslation("tickets");
  const bodyOf = (x: Template) => (x.body ? tr(x.body) : "");
  const catLabel = (k: string) => (CAT_LABEL[k] ? tr(CAT_LABEL[k]) : k);
  const { me, can } = useAuth();
  const server = useActiveServer();
  const { refresh } = useOverview();
  const toast = useToast();
  const manage = me?.role === "admin" || can("can_manage_tickets");

  const [list, setList] = useState<any[]>([]);
  const [sel, setSel] = useState<any | null>(null);
  const [creating, setCreating] = useState(false);
  const [tpl, setTpl] = useState<Template>(TEMPLATES[0]);
  const [form, setForm] = useState(() => ({ title: "", workshop_url: "", mod_id: "", body: bodyOf(TEMPLATES[0]) }));
  const [comment, setComment] = useState("");
  const [fStatus, setFStatus] = useState("");
  const [fCat, setFCat] = useState("");
  const [err, setErr] = useState("");

  async function load() {
    try {
      setList(await apiJson<any[]>("/tickets"));
    } catch (e: any) {
      setErr(e.message);
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function openTicket(id: number) {
    try {
      setSel(await apiJson<any>(`/tickets/${id}`));
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  function pickTemplate(t: Template) {
    setTpl(t);
    setForm({ title: "", workshop_url: "", mod_id: "", body: bodyOf(t) });
  }

  async function create(e: FormEvent) {
    e.preventDefault();
    try {
      // apiJson throws on HTTP errors (api() does not) — otherwise a 401/5xx would report "ticket
      // created", close the form and discard the typed text although nothing was saved.
      await apiJson("/tickets", {
        method: "POST",
        body: JSON.stringify({
          title: form.title,
          body: form.body,
          category: tpl.key,
          workshop_url: tpl.showWorkshop ? form.workshop_url : "",
          mod_id: tpl.showWorkshop ? form.mod_id : "",
          // Which server this is about — without it every mod request would land in the draft
          // of the default server, whatever game the requester plays.
          server_key: server?.key ?? "",
        }),
      });
      toast.success(tr("create.done"));
      setCreating(false);
      pickTemplate(TEMPLATES[0]);
      load();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function addComment() {
    if (!comment.trim() || !sel?.ticket) return;
    try {
      await apiJson(`/tickets/${sel.ticket.id}/comment`, {
        method: "POST",
        body: JSON.stringify({ body: comment }),
      });
      setComment("");
      openTicket(sel.ticket.id);
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function setStatus(s: string) {
    if (!sel?.ticket) return;
    try {
      await apiJson(`/tickets/${sel.ticket.id}/status`, { method: "PUT", body: JSON.stringify({ status: s }) });
      openTicket(sel.ticket.id);
      load();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  async function applyMod() {
    if (!sel?.ticket) return;
    try {
      await apiJson(`/tickets/${sel.ticket.id}/apply-mod`, { method: "POST" });
      toast.success(tr("applyMod.done"));
      openTicket(sel.ticket.id);
      load();
      refresh();
    } catch (e: any) {
      toast.error(e.message);
    }
  }

  const filtered = useMemo(
    () =>
      list.filter(
        (t) => (!fStatus || t.status === fStatus) && (!fCat || t.category === fCat),
      ),
    [list, fStatus, fCat],
  );

  const t = sel?.ticket;
  const wid = tpl.showWorkshop ? parseWorkshopId(form.workshop_url) : "";

  return (
    <div className="space-y-5">
      <SectionTitle
        icon={TicketIcon}
        sub={tr("subtitle")}
        right={
          <Button onClick={() => setCreating((v) => !v)}>
            <Plus className="h-4 w-4" /> {tr("create.new")}
          </Button>
        }
      >
        {tr("title")}
      </SectionTitle>

      {err && <Alert>{err}</Alert>}

      {creating && (
        <Card className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">{tr("create.pickTemplate")}</div>
          <div className="flex flex-wrap gap-2">
            {TEMPLATES.map((x) => (
              <button
                key={x.key}
                onClick={() => pickTemplate(x)}
                className={cx(
                  "flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm transition",
                  tpl.key === x.key
                    ? "border-danger bg-danger-soft/40 text-fg"
                    : "border-line text-fg-muted hover:bg-surface-card hover:text-fg",
                )}
              >
                <x.icon className="h-4 w-4" /> {tr(x.label)}
              </button>
            ))}
          </div>

          <form onSubmit={create} className="space-y-3">
            <Field label={tr("form.title")}>
              <Input
                value={form.title}
                placeholder={tr(tpl.titlePlaceholder)}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                required
              />
            </Field>
            {tpl.showWorkshop && (
              <>
                <Field label={tr("form.workshopLink")} hint={wid ? tr("form.workshopIdFound", { id: wid }) : tr("form.workshopIdHint")}>
                  <Input
                    value={form.workshop_url}
                    placeholder="https://steamcommunity.com/sharedfiles/filedetails/?id=…"
                    onChange={(e) => setForm({ ...form, workshop_url: e.target.value })}
                  />
                </Field>
                <Field
                  label={tr("form.modId")}
                  hint={tr("form.modIdHint")}
                >
                  <Input
                    value={form.mod_id}
                    placeholder={tr("form.modIdPlaceholder")}
                    onChange={(e) => setForm({ ...form, mod_id: e.target.value })}
                  />
                </Field>
              </>
            )}
            <Field label={tr("form.description")}>
              <Textarea
                value={form.body}
                onChange={(e) => setForm({ ...form, body: e.target.value })}
                className="h-40 font-mono text-xs"
              />
            </Field>
            <div className="flex gap-2">
              <Button type="submit">{tr("create.submit")}</Button>
              <GhostButton type="button" onClick={() => setCreating(false)}>
                {tr("common:action.cancel")}
              </GhostButton>
            </div>
          </form>
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Select value={fCat} onChange={(e) => setFCat(e.target.value)}>
          <option value="">{tr("filter.allCategories")}</option>
          {Object.keys(CAT_LABEL).map((k) => (
            <option key={k} value={k}>
              {catLabel(k)}
            </option>
          ))}
        </Select>
        <Select value={fStatus} onChange={(e) => setFStatus(e.target.value)}>
          <option value="">{tr("filter.allStatus")}</option>
          {STATUS.map((s) => (
            <option key={s.key} value={s.key}>
              {tr(s.label)}
            </option>
          ))}
        </Select>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* List */}
        <div className="space-y-1.5">
          {filtered.map((x) => {
            const st = statusOf(x.status);
            const active = t?.id === x.id;
            return (
              <button
                key={x.id}
                onClick={() => openTicket(x.id)}
                className={cx(
                  "flex w-full items-center justify-between gap-2 rounded-md border px-3 py-2 text-left text-sm transition",
                  active
                    ? "border-danger bg-surface-sunken"
                    : "border-line-soft bg-surface-card/50 hover:bg-surface-sunken",
                )}
              >
                <span className="min-w-0">
                  <span className="text-fg-dim">#{x.id}</span> <span className="text-fg">{x.title}</span>
                  <span className="mt-0.5 flex items-center gap-1.5">
                    <Badge tone="info">{catLabel(x.category)}</Badge>
                    {x.applied_at && <Badge tone="toxic">{tr("applied.badge")}</Badge>}
                  </span>
                </span>
                <Badge tone={st.tone}>{tr(st.label)}</Badge>
              </button>
            );
          })}
          {filtered.length === 0 && (
            <Card>
              <EmptyState icon={TicketIcon}>{tr("empty")}</EmptyState>
            </Card>
          )}
        </div>

        {/* Detail */}
        <div>
          {t ? (
            <Card className="space-y-3">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <h2 className="text-stencil text-lg text-fg">
                    #{t.id} {t.title}
                  </h2>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5">
                    <Badge tone="info">{catLabel(t.category)}</Badge>
                    <Badge tone={statusOf(t.status).tone}>{tr(statusOf(t.status).label)}</Badge>
                    <span className="text-xs text-fg-dim">
                      {t.author_name} · {fmtRelative(t.created_at, true)}
                    </span>
                  </div>
                </div>
                {t.applied_at && <span className="stamp border-success-border text-success-text">{tr("applied.stamp")}</span>}
              </div>

              {t.category === "mod_request" && (
                <div className="space-y-1 rounded-md border border-line-soft bg-surface-card/50 p-2 text-sm">
                  {t.workshop_url &&
                    // The requester types `workshop_url` — an admin reads it here. A `javascript:`
                    // scheme would run in THEIR session, and the access token is in localStorage.
                    // A link that is not http(s) is shown as text but not linked: hiding it
                    // would be wrong, making it clickable too.
                    (safeUrl(t.workshop_url) ? (
                      <a
                        href={safeUrl(t.workshop_url)}
                        target="_blank"
                        rel="noreferrer"
                        className="flex items-center gap-1.5 break-all text-success-text hover:underline"
                      >
                        <ExternalLink className="h-3.5 w-3.5 shrink-0" /> {t.workshop_url}
                      </a>
                    ) : (
                      <div
                        className="flex items-center gap-1.5 break-all text-fg-dim"
                        title={tr("detail.notALink")}
                      >
                        <ExternalLink className="h-3.5 w-3.5 shrink-0" /> {t.workshop_url}
                      </div>
                    ))}
                  <div className="flex flex-wrap gap-x-4 text-xs text-fg-muted">
                    <span>
                      {tr("detail.workshopId")} <span className="font-mono text-fg">{t.workshop_id || "—"}</span>
                    </span>
                    <span>
                      {tr("detail.modId")} <span className="font-mono text-fg">{t.mod_id || "—"}</span>
                    </span>
                  </div>
                </div>
              )}

              <p className="whitespace-pre-wrap text-sm text-fg-muted">{t.body}</p>

              {manage && (
                <div className="space-y-2 border-t border-line-soft pt-3">
                  <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">{tr("detail.setStatus")}</div>
                  <div className="flex flex-wrap gap-1.5">
                    {STATUS.map((s) => (
                      <button
                        key={s.key}
                        onClick={() => setStatus(s.key)}
                        className={cx(
                          "rounded-md border px-2.5 py-1 text-xs transition",
                          t.status === s.key
                            ? "border-danger bg-danger-soft/40 text-fg"
                            : "border-line text-fg-muted hover:bg-surface-card hover:text-fg",
                        )}
                      >
                        {tr(s.label)}
                      </button>
                    ))}
                  </div>
                  {t.category === "mod_request" && t.status === "approved" && can("can_manage_mods") && (
                    <Button tone="toxic" onClick={applyMod}>
                      <Check className="h-4 w-4" /> {tr("applyMod.label")}
                    </Button>
                  )}
                </div>
              )}

              <div className="space-y-2 border-t border-line-soft pt-3">
                <div className="text-xs font-semibold uppercase tracking-[0.2em] text-fg-dim">
                  {tr("comments.title", { n: sel?.comments?.length ?? 0 })}
                </div>
                {(sel?.comments ?? []).map((c: any) => (
                  <div key={c.id} className="rounded-md border border-surface-card bg-surface-card/40 px-2.5 py-1.5 text-sm">
                    <div className="text-xs text-fg-dim">
                      {c.author_name} · {fmtRelative(c.created_at, true)}
                    </div>
                    <div className="whitespace-pre-wrap text-fg-muted">{c.body}</div>
                  </div>
                ))}
                <div className="flex gap-2">
                  <Input
                    value={comment}
                    onChange={(e) => setComment(e.target.value)}
                    placeholder={tr("comments.placeholder")}
                    onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && (e.preventDefault(), addComment())}
                  />
                  <Button onClick={addComment}>
                    <Send className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            </Card>
          ) : (
            <Card>
              <EmptyState icon={TicketIcon}>{tr("detail.pick")}</EmptyState>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
