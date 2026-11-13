const { createApp } = Vue;

createApp({
  data() {
    return {
      me: "",
      items: [],
      specs: [],
      current: null,
      preview: null,
      form: { shape: "wide", spec_name: "", metrics: "", required_cols: "" },
      busy: false,
      msg: "",
      msgErr: false,
    };
  },
  async mounted() {
    const me = await fetch("/api/me");
    if (!me.ok) {
      location.href = "/login";
      return;
    }
    this.me = (await me.json()).user;
    await this.refresh();
  },
  methods: {
    tagClass(status) {
      if (status === "已落库") return "ok";
      if (status === "部分落库") return "warn";
      if (status === "落库失败") return "err";
      if (status === "落库中") return "warn";
      if (status === "已清洗待落库" || status === "已抽取待清洗") return "warn";
      return "";
    },
    shapeTag(shape) {
      if (shape === "wide") return "shape-wide";
      if (shape === "narrow") return "shape-narrow";
      return "";
    },
    say(text, isErr) {
      this.msg = text;
      this.msgErr = !!isErr;
    },
    async handle(res, okText) {
      const data = await res.json();
      if (!res.ok) {
        if (data && data.item) this.current = data.item;
        this.say((data && data.error) || "操作失败", true);
        await this.refresh();
        await this.loadPreview();
        return null;
      }
      this.current = data;
      if (okText) this.say(okText, false);
      await this.refresh();
      await this.loadPreview();
      return data;
    },
    async refresh() {
      const res = await fetch("/api/pipelines");
      if (res.ok) this.items = (await res.json()).items || [];
      const sp = await fetch("/api/shape-specs");
      if (sp.ok) this.specs = (await sp.json()).items || [];
    },
    async loadPreview() {
      if (!this.current) return;
      const res = await fetch(`/api/pipelines/${this.current.id}/preview`);
      if (res.ok) this.preview = await res.json();
    },
    fillForm() {
      const s = this.current && this.current.spec;
      this.form = {
        shape: s ? s.shape : "wide",
        spec_name: s ? s.spec_name : "",
        metrics: s ? s.metrics.join(",") : "",
        required_cols: s ? s.required_cols.join(",") : "",
      };
    },
    async open(id) {
      const res = await fetch(`/api/pipelines/${id}`);
      if (!res.ok) return;
      this.current = await res.json();
      this.msg = "";
      this.fillForm();
      await this.loadPreview();
    },
    async extract() {
      if (!this.current) return;
      this.busy = true;
      try {
        const res = await fetch(`/api/pipelines/${this.current.id}/extract`, { method: "POST" });
        await this.handle(res, "抽取完成，请继续清洗");
      } finally {
        this.busy = false;
      }
    },
    async clean() {
      if (!this.current) return;
      this.busy = true;
      try {
        const res = await fetch(`/api/pipelines/${this.current.id}/clean`, { method: "POST" });
        await this.handle(res, "清洗通过，可以落库");
      } finally {
        this.busy = false;
      }
    },
    async load() {
      if (!this.current) return;
      this.busy = true;
      try {
        const res = await fetch(`/api/pipelines/${this.current.id}/load`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ shape: this.form.shape }),
        });
        const data = await this.handle(res, "");
        if (data) {
          const st = data.status;
          this.say(st === "部分落库" ? "部分落库，未落指标已列出" : "落库完成", st !== "已落库");
        }
      } finally {
        this.busy = false;
      }
    },
    async saveShape() {
      if (!this.current) return;
      this.busy = true;
      try {
        const res = await fetch(`/api/pipelines/${this.current.id}/shape`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.form),
        });
        await this.handle(res, "形态已写进说明，本日禁止改口");
      } finally {
        this.busy = false;
      }
    },
    async logout() {
      await fetch("/api/logout", { method: "POST" });
      location.href = "/login";
    },
  },
}).mount("#app");
