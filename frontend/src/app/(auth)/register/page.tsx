"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { registerErrorMessage } from "@/components/auth/auth-errors";
import { AuthNotice } from "@/components/auth/auth-notice";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/lib/auth/auth-provider";
import {
  VALIDATION,
  validateEmail,
  validateNewPassword,
  validateSlug,
  validateTenantName,
  type FieldError,
} from "@/lib/validation";

interface RegisterErrors {
  name: FieldError;
  slug: FieldError;
  email: FieldError;
  password: FieldError;
}

const NO_ERRORS: RegisterErrors = { name: null, slug: null, email: null, password: null };

export default function RegisterPage() {
  const { register } = useAuth();
  const router = useRouter();

  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<RegisterErrors>(NO_ERRORS);
  // After the first submit attempt, each edit re-validates its own field so
  // mistakes clear as they are fixed.
  const [validated, setValidated] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  function updateName(value: string) {
    setName(value);
    if (validated) setErrors((previous) => ({ ...previous, name: validateTenantName(value) }));
  }

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
      setErrors((previous) => ({ ...previous, password: validateNewPassword(value) }));
    }
  }

  async function submit() {
    setSubmitting(true);
    setFormError(null);
    try {
      // The backend creates the workspace and returns tokens: we are signed in
      // as its first admin, so straight to the dashboard.
      await register({
        name: name.trim(),
        slug: slug.trim().toLowerCase(),
        admin_email: email.trim().toLowerCase(),
        admin_password: password,
      });
      router.push("/dashboard");
    } catch (error) {
      setFormError(registerErrorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    const next: RegisterErrors = {
      name: validateTenantName(name),
      slug: validateSlug(slug),
      email: validateEmail(email),
      password: validateNewPassword(password),
    };
    setValidated(true);
    setErrors(next);
    if (next.name || next.slug || next.email || next.password) return;
    void submit();
  }

  return (
    <Card>
      <CardHeader className="text-center">
        <h2 className="text-xl font-semibold text-foreground">Create your workspace</h2>
        <CardDescription>
          Set up a workspace and the first administrator account for your organization.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {formError ? <AuthNotice tone="error" message={formError} onDismiss={() => setFormError(null)} /> : null}

        <form onSubmit={handleSubmit} className="space-y-4" noValidate>
          <Field label="Organization name" htmlFor="register-name" error={errors.name}>
            <Input
              id="register-name"
              name="name"
              value={name}
              onChange={(event) => updateName(event.target.value)}
              autoComplete="organization"
              aria-invalid={Boolean(errors.name)}
              disabled={submitting}
            />
          </Field>
          <Field
            label="Workspace slug"
            htmlFor="register-slug"
            hint="Used in your workspace URL, e.g. acme-corp"
            error={errors.slug}
          >
            <Input
              id="register-slug"
              name="slug"
              value={slug}
              onChange={(event) => updateSlug(event.target.value)}
              autoComplete="organization"
              autoCapitalize="none"
              spellCheck={false}
              aria-invalid={Boolean(errors.slug)}
              disabled={submitting}
            />
          </Field>
          <Field label="Admin email" htmlFor="register-email" error={errors.email}>
            <Input
              id="register-email"
              name="admin_email"
              type="email"
              value={email}
              onChange={(event) => updateEmail(event.target.value)}
              autoComplete="email"
              aria-invalid={Boolean(errors.email)}
              disabled={submitting}
            />
          </Field>
          <Field
            label="Admin password"
            htmlFor="register-password"
            hint={`At least ${VALIDATION.newPasswordMin} characters.`}
            error={errors.password}
          >
            <Input
              id="register-password"
              name="admin_password"
              type="password"
              value={password}
              onChange={(event) => updatePassword(event.target.value)}
              autoComplete="new-password"
              aria-invalid={Boolean(errors.password)}
              disabled={submitting}
            />
          </Field>
          <Button type="submit" size="lg" className="w-full" loading={submitting}>
            Create workspace
          </Button>
        </form>

        <p className="text-center text-sm text-muted">
          Already have a workspace?{" "}
          <Link href="/login" className="font-medium text-primary hover:underline">
            Sign in
          </Link>
        </p>
      </CardContent>
    </Card>
  );
}
