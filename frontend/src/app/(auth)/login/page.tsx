"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { loginErrorMessage } from "@/components/auth/auth-errors";
import { AuthNotice } from "@/components/auth/auth-notice";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/lib/auth/auth-provider";
import { consumeSessionExpiredFlag } from "@/lib/auth/tokens";
import {
  validateEmail,
  validateLoginPassword,
  validateSlug,
  type FieldError,
} from "@/lib/validation";

interface LoginErrors {
  slug: FieldError;
  email: FieldError;
  password: FieldError;
}

const NO_ERRORS: LoginErrors = { slug: null, email: null, password: null };

export default function LoginPage() {
  const { status, login } = useAuth();
  const router = useRouter();

  const [slug, setSlug] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<LoginErrors>(NO_ERRORS);
  // Becomes true after the first submit attempt; from then on each edit
  // re-validates its own field so mistakes clear as they are fixed.
  const [validated, setValidated] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  // Consumed exactly once (localStorage → in-memory): a refresh failure marks
  // the session as expired so we can say *why* the user is back here.
  const [sessionExpired, setSessionExpired] = useState(() => consumeSessionExpiredFlag());

  // Already signed in (deep link, back button, another tab): no form to show.
  useEffect(() => {
    if (status === "authenticated") router.replace("/dashboard");
  }, [status, router]);

  function updateSlug(value: string) {
    setSlug(value);
    if (validated) setErrors((previous) => ({ ...previous, slug: validateSlug(value) }));
  }

  function updateEmail(value: string) {
    setEmail(value);
    if (validated) setErrors((previous) => ({ ...previous, email: validateEmail(value) }));
  }

  function updatePassword(value: string) {
    setPassword(value);
    if (validated) {
      setErrors((previous) => ({ ...previous, password: validateLoginPassword(value) }));
    }
  }

  async function submit() {
    setSubmitting(true);
    setFormError(null);
    try {
      await login(slug.trim().toLowerCase(), email.trim().toLowerCase(), password);
      router.push("/dashboard");
    } catch (error) {
      setFormError(loginErrorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    const next: LoginErrors = {
      slug: validateSlug(slug),
      email: validateEmail(email),
      password: validateLoginPassword(password),
    };
    setValidated(true);
    setErrors(next);
    if (next.slug || next.email || next.password) return;
    void submit();
  }

  return (
    <Card>
      <CardHeader className="text-center">
        <h2 className="text-xl font-semibold text-foreground">Sign in</h2>
        <CardDescription>Sign in to your TenantMind workspace.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {sessionExpired ? (
          <AuthNotice message="Your session has expired. Please sign in again." onDismiss={() => setSessionExpired(false)} />
        ) : null}
        {formError ? <AuthNotice tone="error" message={formError} onDismiss={() => setFormError(null)} /> : null}

        <form onSubmit={handleSubmit} className="space-y-4" noValidate>
          <Field label="Workspace slug" htmlFor="login-slug" error={errors.slug}>
            <Input
              id="login-slug"
              name="tenant_slug"
              value={slug}
              onChange={(event) => updateSlug(event.target.value)}
              autoComplete="organization"
              autoCapitalize="none"
              spellCheck={false}
              aria-invalid={Boolean(errors.slug)}
              disabled={submitting}
            />
          </Field>
          <Field label="Email" htmlFor="login-email" error={errors.email}>
            <Input
              id="login-email"
              name="email"
              type="email"
              value={email}
              onChange={(event) => updateEmail(event.target.value)}
              autoComplete="email"
              aria-invalid={Boolean(errors.email)}
              disabled={submitting}
            />
          </Field>
          <Field label="Password" htmlFor="login-password" error={errors.password}>
            <Input
              id="login-password"
              name="password"
              type="password"
              value={password}
              onChange={(event) => updatePassword(event.target.value)}
              autoComplete="current-password"
              aria-invalid={Boolean(errors.password)}
              disabled={submitting}
            />
          </Field>
          <Button type="submit" size="lg" className="w-full" loading={submitting}>
            Sign in
          </Button>
        </form>

        <p className="text-center text-sm text-muted">
          New workspace?{" "}
          <Link href="/register" className="font-medium text-primary hover:underline">
            Create one
          </Link>
        </p>
      </CardContent>
    </Card>
  );
}
