"""Herramienta única para levantar y operar el entorno (Argo CD + Argo Workflows + CI con GitHub).

Comandos:
  crear       Deja todo funcionando. Arranca minikube si hace falta, instala Argo CD, crea los Secrets,
              aplica root-app y lanza el primer pipeline de CI (que construye y publica la primera imagen).
              Es repetible: correrlo de nuevo no rompe nada.
  abrir       Abre los port-forward de las UIs (Argo CD y Argo Workflows) y del webhook de GitHub, y muestra
              cómo entrar. Reabre solo los que se cortan. `abrir webhook` abre sólo el del webhook.
  estado      Resumen: Applications, pods con problemas y últimos workflows.
  reiniciar   [laboratorio] Borra el cluster de minikube y lo crea de cero (borrar + crear). Pide confirmación.
  borrar      [laboratorio] Borra el cluster de minikube. Pide confirmación.

Opciones:
  --si        No pedir confirmación (reiniciar, borrar).
  --sin-ci    Con `crear`: no lanzar el primer pipeline.
  --sin-abrir Con `crear`: al terminar, no abrir las UIs (por defecto `crear` termina con `abrir`).
  --navegador Con `abrir` o `crear`: abrir cada URL en el navegador.

Credenciales: se leen de un archivo .env (ver .env.example). Se busca en la carpeta actual, junto a este
script y en las carpetas superiores (por ejemplo repos/.env). El .env NUNCA debe subirse a Git: la herramienta
se niega a usarlo si está dentro de un repo y no figura en .gitignore.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

# ───────────────────────── Configuración ─────────────────────────
OWNER = "diegodevopsorg"    # organización de GitHub dueña de los repos
ARGOCD_VERSION = "v3.6.1"   # versión fija; se sube cambiando esta línea
ARGOCD_MANIFEST = f"https://raw.githubusercontent.com/argoproj/argo-cd/{ARGOCD_VERSION}/manifests/install.yaml"
APP = "simple-python-app"   # app cuyo CI se habilita (repos <app> y <app>-conf)
PERFIL_MINIKUBE = "minikube"

# Repos que tiene que poder leer el token de GitHub (un solo token para todos)
REPOS = ["argocd-deploy", f"{APP}-conf", APP]
REQUERIDAS = ["GH_TOKEN", "DOCKERHUB_USER", "DOCKERHUB_TOKEN"]
AQUI = os.path.dirname(os.path.abspath(__file__))

DURACION_TOKEN = "8h"        # vigencia del token de la UI de Argo Workflows
ESPERA_ARRANQUE = 2          # segundos para detectar un port-forward que falla al iniciar
REVISION = 5                 # cada cuántos segundos se verifica que los port-forward sigan vivos
ESPERA_PODS = 180            # segundos que se espera a que un pod termine de arrancar
ESPERA_PLANTILLA_CI = 600    # segundos que se espera a que exista el WorkflowTemplate del CI
ESPERA_EVENTBUS = 420        # segundos que se espera a que el EventBus (NATS JetStream) quede sincronizado
PODS_ARRANCANDO = ("pod is not running", "no endpoints available", "ContainerCreating")


# ───────────────────────── Utilidades ─────────────────────────
def paso(texto):
    print(f"\n▶ {texto}", flush=True)


def fallar(texto):
    print(f"\n❌ {texto}", flush=True)
    sys.exit(1)


def url_repo(repo):
    return f"https://github.com/{OWNER}/{repo}.git"


def comando(sub):
    """El comando exacto para volver a llamar a esta herramienta, con la ruta con la que se la invocó
    (cambia según la carpeta desde la que se corra, así que no se puede escribir fijo)."""
    return f"python {sys.argv[0]} {sub}"


def kubectl(*args, entrada=None, verificar=True):
    r = subprocess.run(["kubectl", *args], input=entrada, capture_output=True, text=True)
    if verificar and r.returncode != 0:
        fallar(f"kubectl {' '.join(args[:3])}... falló:\n{r.stderr.strip()}")
    return r


def aplicar(manifiesto):
    """Aplica un manifiesto por stdin: los secretos nunca quedan en la línea de comandos."""
    kubectl("apply", "-f", "-", entrada=json.dumps(manifiesto))


def contexto_actual():
    return kubectl("config", "current-context", verificar=False).stdout.strip()


def cluster_responde():
    return kubectl("cluster-info", verificar=False).returncode == 0


def confirmar(pregunta, args):
    if "--si" in args:
        return
    if input(f"{pregunta}\n   Escribí 'si' para continuar: ").strip().lower() != "si":
        fallar("Cancelado. No se hizo ningún cambio.")


# ───────────────────────── Archivo .env ─────────────────────────
def parsear_env(texto):
    """KEY=VALOR por línea. Se ignoran líneas vacías y las que empiezan con #. Sin comentarios al final de la línea."""
    valores = {}
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        if linea.startswith("export "):
            linea = linea[len("export "):]
        clave, valor = linea.split("=", 1)
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        valores[clave.strip()] = valor
    return valores


def buscar_envs():
    """Todos los .env que encuentra, en orden de prioridad: carpeta actual, junto al script y 3 carpetas superiores."""
    candidatos = [os.path.join(os.getcwd(), ".env"), os.path.join(AQUI, ".env")]
    carpeta = AQUI
    for _ in range(3):
        carpeta = os.path.dirname(carpeta)
        candidatos.append(os.path.join(carpeta, ".env"))
    vistos, encontrados = set(), []
    for c in candidatos:
        real = os.path.normcase(os.path.realpath(c))
        if os.path.isfile(c) and real not in vistos:
            vistos.add(real)
            encontrados.append(c)
    return encontrados


def buscar_env():
    """El .env que se usa: el de mayor prioridad de los que existen."""
    encontrados = buscar_envs()
    return encontrados[0] if encontrados else None


def env_ignorado_por_git(ruta):
    """False sólo si el archivo está dentro de un repo git y NO está ignorado (riesgo de subir los tokens)."""
    if shutil.which("git") is None:
        return True
    r = subprocess.run(["git", "-C", os.path.dirname(ruta), "check-ignore", "-q", "--", os.path.basename(ruta)],
                       capture_output=True)
    return r.returncode != 1   # 0 = ignorado, 128 = no es un repo; 1 = en un repo y sin ignorar


def cargar_env(obligatorio):
    """Carga el .env en os.environ (sus valores mandan sobre variables viejas de la sesión de PowerShell)."""
    ruta = buscar_env()
    if ruta is None:
        if obligatorio:
            fallar("No encontré el archivo .env.\n"
                   f"   Copiá {os.path.join(AQUI, '.env.example')} como .env (en repos/) y completalo.")
        return
    if not env_ignorado_por_git(ruta):
        fallar(f"{ruta} está dentro de un repo git y NO figura en .gitignore: podrías subir tus tokens por error.\n"
               "   Agregá una línea '.env' al .gitignore de esa carpeta y volvé a correr.")
    otros = [p for p in buscar_envs() if p != ruta]
    if otros:
        # Con dos .env, cuál manda depende de la carpeta desde la que se corra: una fuente de errores silenciosos
        # (por ejemplo, recrear el cluster con la memoria de un archivo viejo).
        print(f"   ⚠️  Hay más de un .env. Se usa el primero: {ruta}")
        for p in otros:
            print(f"      también existe: {p}")
        print("      Dejá sólo uno (repos/.env) para que no dependa de la carpeta desde la que corras la herramienta.")
    valores = parsear_env(open(ruta, encoding="utf-8").read())
    for k, v in valores.items():
        if v:
            os.environ[k] = v
    print(f"   🔑 Credenciales leídas de {ruta}")


def exigir_variables():
    faltan = [v for v in REQUERIDAS if not os.environ.get(v)]
    if faltan:
        fallar("Faltan variables en el .env (o están vacías): " + ", ".join(faltan))
    # Tokens cruzados (ya pasó con otro proveedor): el prefijo delata de quién es cada uno.
    if os.environ["GH_TOKEN"].startswith("dckr_pat_"):
        fallar("GH_TOKEN tiene un token de Docker Hub (empieza con 'dckr_pat_'). Tiene que ser el token de GitHub.")
    if os.environ["DOCKERHUB_TOKEN"].startswith(("github_pat_", "ghp_", "gho_", "ghs_")):
        fallar("DOCKERHUB_TOKEN tiene un token de GitHub. Tiene que ser el access token de Docker Hub (dckr_pat_...).")


# ───────────────────────── Verificaciones previas ─────────────────────────
def git_ls_remote(repo, token, *args):
    """git ls-remote con el token en una cabecera por variables de entorno (no aparece en la línea de comandos)."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="http.extraHeader", GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
    return subprocess.run(["git", "ls-remote", "--exit-code", url_repo(repo), *args],
                          env=env, capture_output=True, text=True)


def repo_legible(repo, token):
    return git_ls_remote(repo, token, "HEAD").returncode == 0


def github_puede_escribir(repo, token):
    """Pregunta a la API de GitHub si el token tiene permiso de escritura en el repo.
    Devuelve True/False, o None si la API no informa los permisos (en ese caso no se puede asegurar nada)."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{OWNER}/{repo}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "entorno.py"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            permisos = json.load(r).get("permissions")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None
    return permisos.get("push") if isinstance(permisos, dict) else None


def dockerhub_valido(usuario, token):
    """Pide a Docker Hub un token de push para la imagen de la app: es lo mismo que hará Kaniko."""
    basic = base64.b64encode(f"{usuario}:{token}".encode()).decode()
    req = urllib.request.Request(
        "https://auth.docker.io/token?service=registry.docker.io"
        f"&scope=repository:{usuario}/{APP}:push,pull",
        headers={"Authorization": f"Basic {basic}"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True, ""
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"sin conexión con Docker Hub ({e.reason})"


def verificaciones_previas():
    """Todo lo que se puede comprobar SIN tocar el cluster. Si algo falla, no se arranca nada."""
    paso("1/5 Verificaciones previas (todavía no se toca nada)")
    exigir_variables()
    for repo in REPOS:
        if not repo_legible(repo, os.environ["GH_TOKEN"]):
            fallar(f"No se puede leer {url_repo(repo)} con GH_TOKEN.\n"
                   "   ¿Existe el repo, tiene al menos un commit en main y el token tiene acceso a ESE repo?\n"
                   "   (En el token: Repository access debe incluirlo, y Contents: Read and write.)")
        print(f"   ✅ {repo} legible")
    # El CI escribe el tag en el repo -conf: un token de sólo lectura fallaría recién en el último paso del pipeline.
    puede = github_puede_escribir(f"{APP}-conf", os.environ["GH_TOKEN"])
    if puede is False:
        fallar(f"GH_TOKEN puede leer {APP}-conf pero NO escribir. El CI necesita escribir el tag.\n"
               "   En el token: Permissions → Repository permissions → Contents: Read and write.")
    print(f"   ✅ GH_TOKEN puede escribir en {APP}-conf" if puede
          else f"   ⚠️  No pude comprobar el permiso de escritura en {APP}-conf (si falla el CI en 'release', revisalo)")
    ok, motivo = dockerhub_valido(os.environ["DOCKERHUB_USER"], os.environ["DOCKERHUB_TOKEN"])
    if not ok:
        fallar(f"Docker Hub rechazó DOCKERHUB_USER / DOCKERHUB_TOKEN ({motivo}).\n"
               "   DOCKERHUB_TOKEN tiene que ser un access token de Docker Hub (empieza con 'dckr_pat_'), con permiso\n"
               "   Read & Write. Se crea en Docker Hub: Account settings → Personal access tokens.")
    print("   ✅ Docker Hub acepta las credenciales")


# ───────────────────────── Cluster ─────────────────────────
def asegurar_cluster():
    paso("2/5 Cluster")
    if cluster_responde():
        print(f"   ✅ Cluster accesible (contexto: {contexto_actual()})")
        return
    ctx = contexto_actual()
    if ctx not in ("", PERFIL_MINIKUBE):
        fallar(f"El contexto actual es '{ctx}' y no responde. No arranco minikube sobre otro contexto.")
    if shutil.which("minikube") is None:
        fallar("El cluster no responde y minikube no está instalado.")
    print("   El cluster no responde: arrancando minikube (la primera vez descarga imágenes, tarda unos minutos)...")
    cmd = ["minikube", "start", f"--profile={PERFIL_MINIKUBE}", "--driver=docker"]
    if os.environ.get("MINIKUBE_CPUS"):
        cmd.append(f"--cpus={os.environ['MINIKUBE_CPUS']}")
    if os.environ.get("MINIKUBE_MEMORY"):
        cmd.append(f"--memory={os.environ['MINIKUBE_MEMORY']}")
    if subprocess.run(cmd).returncode != 0:
        fallar("minikube no pudo arrancar. ¿Está Docker Desktop abierto?")
    if not cluster_responde():
        fallar("minikube arrancó pero el cluster no responde.")
    print(f"   ✅ Cluster listo (contexto: {contexto_actual()})")


def namespace(nombre):
    aplicar({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": nombre}})


def secret(nombre, ns, string_data=None, data=None, tipo="Opaque", labels=None):
    m = {"apiVersion": "v1", "kind": "Secret", "type": tipo,
         "metadata": {"name": nombre, "namespace": ns, "labels": labels or {}}}
    if string_data:
        m["stringData"] = string_data
    if data:
        m["data"] = data
    aplicar(m)


def instalar_argocd():
    paso(f"3/5 Instalando Argo CD {ARGOCD_VERSION}")
    namespace("argocd")
    # server-side: el CRD de ApplicationSet es demasiado grande para el apply normal
    kubectl("apply", "-n", "argocd", "--server-side", "--force-conflicts", "-f", ARGOCD_MANIFEST)
    kubectl("wait", "--for=condition=available", "deployment", "--all", "-n", "argocd", "--timeout=300s")
    kubectl("rollout", "status", "statefulset/argocd-application-controller", "-n", "argocd", "--timeout=300s")
    print("   ✅ Argo CD listo")


def crear_secrets():
    paso("4/5 Creando Secrets desde el .env")
    # Argo CD: UNA sola credencial que vale para todos los repos de la organización (plantilla "repo-creds":
    # se aplica a cualquier repo cuya URL empiece con la indicada). Agregar un repo nuevo no requiere nada.
    secret("repo-creds-github", "argocd",
           labels={"argocd.argoproj.io/secret-type": "repo-creds"},
           string_data={"type": "git", "url": f"https://github.com/{OWNER}",
                        "username": "x-access-token", "password": os.environ["GH_TOKEN"]})
    print(f"   ✅ Argo CD puede leer todos los repos de {OWNER}")

    # CI: UN solo Secret con el token de GitHub (el pipeline lo usa para clonar el código y publicar el tag)
    namespace("argo")
    secret("github-token", "argo", string_data={"token": os.environ["GH_TOKEN"]})
    usuario, token = os.environ["DOCKERHUB_USER"], os.environ["DOCKERHUB_TOKEN"]
    auth = base64.b64encode(f"{usuario}:{token}".encode()).decode()
    config = {"auths": {"https://index.docker.io/v1/": {"username": usuario, "password": token, "auth": auth}}}
    secret("dockerhub-creds", "argo", tipo="kubernetes.io/dockerconfigjson",
           data={".dockerconfigjson": base64.b64encode(json.dumps(config).encode()).decode()})
    print("   ✅ Secrets del CI creados en el namespace argo")



def aplicar_root_app():
    paso("5/5 Aplicando root-app (a partir de acá todo sale de Git)")
    kubectl("apply", "-f", os.path.join(AQUI, "root-app.yaml"))
    estado = ""
    for _ in range(36):  # hasta 3 minutos
        estado = kubectl("get", "application", "root-app-manager", "-n", "argocd",
                         "-o", "jsonpath={.status.sync.status}/{.status.health.status}", verificar=False).stdout
        if estado == "Synced/Healthy":
            print("   ✅ root-app-manager Synced/Healthy")
            break
        time.sleep(5)
    else:
        print(f"   ⚠️  root-app-manager todavía en '{estado or 'sin estado'}'. Revisalo con: {comando('estado')}")

    # Synced no alcanza: el repo puede tener archivos duplicados o de una versión anterior mezclados.
    avisos = kubectl("get", "application", "root-app-manager", "-n", "argocd", "-o",
                     'jsonpath={range .status.conditions[*]}{.type}: {.message}{"\\n"}{end}',
                     verificar=False).stdout.strip()
    if avisos:
        print("\n   ⚠️  root-app-manager tiene advertencias. El repo argocd-deploy probablemente tiene archivos viejos mezclados:")
        for linea in avisos.splitlines():
            print(f"      - {linea}")
        print("      Para ver de dónde sale cada cosa: kubectl get applications,applicationsets -n argocd")


def primer_ci():
    """Lanza el primer pipeline: construye la primera imagen y escribe su tag en el repo -conf.
    Así no hay que construir ni subir la imagen a mano."""
    paso("Primer pipeline de CI")
    if kubectl("get", "workflows", "-n", "argo", "-o", "name", verificar=False).stdout.strip():
        print("   ⏭️  Ya hay workflows en el cluster: no lanzo otro.")
        return
    print("   Esperando a que el CI esté instalado (WorkflowTemplate ci-build-release)...")
    limite = time.time() + ESPERA_PLANTILLA_CI
    while kubectl("get", "workflowtemplate", "ci-build-release", "-n", "argo", verificar=False).returncode != 0:
        if time.time() > limite:
            print(f"   ⚠️  El CI no apareció a tiempo. Revisá: {comando('estado')}. Se puede lanzar un pipeline desde la UI.")
            return
        time.sleep(10)
    r = git_ls_remote(APP, os.environ["GH_TOKEN"], "refs/heads/main")
    revision = r.stdout.split()[0] if r.returncode == 0 and r.stdout.split() else ""
    if not revision:
        print(f"   ⚠️  No encontré la rama main de {APP}. No lanzo el pipeline.")
        return
    wf = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Workflow",
          "metadata": {"generateName": "ci-inicial-", "namespace": "argo"},
          "spec": {"workflowTemplateRef": {"name": "ci-build-release"},
                   "arguments": {"parameters": [
                       {"name": "app", "value": APP},
                       {"name": "revision", "value": revision},
                       {"name": "image-namespace", "value": os.environ["DOCKERHUB_USER"]}]}}}
    r = kubectl("create", "-f", "-", entrada=json.dumps(wf), verificar=False)
    if r.returncode != 0:
        print(f"   ⚠️  No pude lanzar el pipeline:\n{r.stderr.strip()}")
        return
    print(f"   ✅ Pipeline lanzado ({r.stdout.strip()}), commit {revision[:7]}.")
    print("      Tarda unos minutos: construye la imagen, la sube y escribe el tag en el repo -conf.")
    print("      Mientras tanto la app puede mostrar ImagePullBackOff (usa el tag inicial): es normal.")


def stream_sincronizado(jsz):
    """True si el /jsz de un nodo NATS muestra el stream de Argo Events con todas sus réplicas al día.
    Sólo el líder del stream informa sus réplicas, así que los demás nodos devuelven False."""
    for cuenta in (jsz or {}).get("account_details") or []:
        for st in cuenta.get("stream_detail") or []:
            cl = st.get("cluster") or {}
            replicas = cl.get("replicas") or []
            if cl.get("leader") and len(replicas) >= 2 and all(r.get("current") for r in replicas):
                return True
    return False


def esperar_eventbus():
    """Espera a que el EventBus esté realmente sano, no sólo con los pods Running.
    Tras arrancar el cluster, los 3 nodos de NATS tardan unos minutos en sincronizarse (el estado no se guarda en disco).
    Hasta entonces el EventSource publica y GitHub recibe 200, pero el Sensor no puede leer y el pipeline no se dispara."""
    paso("Esperando al EventBus (necesario para que el webhook dispare el CI)")
    limite = time.time() + ESPERA_EVENTBUS
    while time.time() < limite:
        for i in range(3):
            r = kubectl("get", "--raw", f"/api/v1/namespaces/argo-events/pods/eventbus-default-js-{i}:8222/proxy/jsz?streams=true",
                        verificar=False)
            try:
                if r.returncode == 0 and stream_sincronizado(json.loads(r.stdout)):
                    print("   ✅ EventBus sincronizado")
                    return
            except ValueError:
                pass
        time.sleep(10)
    print("   ⚠️  El EventBus no terminó de sincronizarse. Hasta entonces un push a GitHub puede no disparar el pipeline.\n"
          "      Esperá unos minutos más; si sigue igual, reinicialo en este orden:\n"
          "        kubectl rollout restart statefulset/eventbus-default-js -n argo-events\n"
          "        kubectl rollout restart deployment -n argo-events -l sensor-name=github-push-main")


def crear(args, verificado=False):
    if not verificado:
        cargar_env(obligatorio=True)
        verificaciones_previas()
    asegurar_cluster()
    instalar_argocd()
    crear_secrets()
    aplicar_root_app()
    if "--sin-ci" not in args:
        primer_ci()
    esperar_eventbus()
    print("\nApplications (las de plataforma tardan unos minutos en quedar Synced):")
    print(kubectl("get", "applications", "-n", "argocd", verificar=False).stdout)
    print("✨ Listo.")
    if "--sin-abrir" in args:
        print(f"   Para abrir las UIs y ver URL, usuario y contraseña: {comando('abrir')}")
        return
    print("   Abriendo las UIs: abajo ves la URL y cómo entrar a cada una.")
    print(f"   Se quedan abiertas mientras esta ventana siga abierta. Para volver a abrirlas: {comando('abrir')}")
    abrir(args)


# ───────────────────────── Operación ─────────────────────────
def credenciales_argocd():
    r = kubectl("get", "secret", "argocd-initial-admin-secret", "-n", "argocd",
                "-o", "jsonpath={.data.password}", verificar=False)
    if r.returncode != 0 or not r.stdout:
        return ["Usuario: admin",
                "Password: no disponible (el secret 'argocd-initial-admin-secret' no existe; "
                "si cambiaste la contraseña, usá la nueva)"]
    return ["Usuario: admin", f"Password: {base64.b64decode(r.stdout).decode('utf-8')}"]


def credenciales_workflows():
    # La UI usa auth-mode "client": no hay usuario/contraseña, se pega un token de ServiceAccount.
    r = kubectl("create", "token", "ui-viewer", "-n", "argo", f"--duration={DURACION_TOKEN}", verificar=False)
    if r.returncode != 0:
        return ["Login: token de ServiceAccount (no hay usuario/contraseña)",
                "Token: no disponible, falta el ServiceAccount 'ui-viewer' en el namespace 'argo'.",
                "       Está declarado en platform/manifests/ci/05-ui-viewer.yaml: revisá que platform-ci esté Synced."]
    return ["Login: token de ServiceAccount (no hay usuario/contraseña)",
            f"Token (válido {DURACION_TOKEN}), pegarlo tal cual en el login:",
            f"Bearer {r.stdout.strip()}"]


def info_webhook():
    return ["Sin login: es el endpoint que recibe los eventos de GitHub (sólo acepta POST).",
            "Para que GitHub llegue hasta acá, en otra terminal: cloudflared tunnel --url http://localhost:12000"]


DASHBOARDS = {
    "argocd": {"nombre": "Argo CD", "namespace": "argocd", "service": "argocd-server",
               "puerto_local": 8080, "puerto_service": 443, "url": "https://localhost:8080",
               "credenciales": credenciales_argocd},
    "workflows": {"nombre": "Argo Workflows", "namespace": "argo", "service": "platform-argo-workflows-server",
                  "puerto_local": 2746, "puerto_service": 2746, "url": "http://localhost:2746",
                  "credenciales": credenciales_workflows},
    # No es una UI: es el puerto al que apunta el túnel público de GitHub. Se mantiene abierto (y se reabre solo
    # si se corta) junto con las UIs; antes era un port-forward manual que se caía sin que nadie lo notara.
    "webhook": {"nombre": "Webhook de GitHub", "namespace": "argo-events", "service": "github-eventsource-svc",
                "puerto_local": 12000, "puerto_service": 12000, "url": "http://localhost:12000/push",
                "credenciales": info_webhook, "navegador": False},
}


def iniciar_port_forward(d):
    return subprocess.Popen(
        ["kubectl", "port-forward", f"svc/{d['service']}", "-n", d["namespace"],
         f"{d['puerto_local']}:{d['puerto_service']}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def abrir_port_forward(d):
    """Abre el port-forward. Si el pod todavía está arrancando, espera y reintenta."""
    limite = time.time() + ESPERA_PODS
    avisado = False
    while True:
        proc = iniciar_port_forward(d)
        time.sleep(ESPERA_ARRANQUE)
        if proc.poll() is None:
            return proc, None
        error = (proc.stderr.read() or "").strip()
        if any(m in error for m in PODS_ARRANCANDO) and time.time() < limite:
            if not avisado:
                print(f"⏳ {d['nombre']}: el pod todavía está arrancando, espero hasta {ESPERA_PODS} s...")
                avisado = True
            time.sleep(5)
            continue
        return None, error


def abrir(args):
    claves = [a for a in args if a in DASHBOARDS] or list(DASHBOARDS)
    if not cluster_responde():
        fallar(f"El cluster no responde. ¿Está arrancado? Probá: {comando('crear')}")
    activos = {}
    for clave in claves:
        d = DASHBOARDS[clave]
        if kubectl("get", "svc", d["service"], "-n", d["namespace"], "-o", "name", verificar=False).returncode != 0:
            print(f"⏭️  {d['nombre']}: no está instalado (no existe svc/{d['service']} en '{d['namespace']}'). Se omite.")
            continue
        proc, error = abrir_port_forward(d)
        if proc is None:
            print(f"❌ {d['nombre']}: no pudo iniciar el port-forward en el puerto {d['puerto_local']}.\n   {error}")
            if "Unable to listen" in error:
                print("   El puerto está ocupado. Si es un port-forward viejo, cerralo con: Get-Process kubectl | Stop-Process")
            elif any(m in error for m in PODS_ARRANCANDO):
                print(f"   El pod no arrancó en {ESPERA_PODS} s. Revisá: kubectl get pods -n {d['namespace']}")
            continue
        activos[clave] = proc
        print(f"\n✅ {d['nombre']}  →  {d['url']}")
        for linea in d["credenciales"]():
            print(f"   {linea}")
        if ("--navegador" in args or "--abrir" in args) and d.get("navegador", True):
            webbrowser.open(d["url"])

    if not activos:
        fallar("No hay ningún dashboard para exponer.")

    print("\nPort-forward activos. Ctrl+C para cerrar todos.")
    try:
        while True:
            time.sleep(REVISION)
            for clave, proc in list(activos.items()):
                if proc.poll() is not None:
                    print(f"🔄 {DASHBOARDS[clave]['nombre']}: el port-forward se cortó. Reabriendo...")
                    activos[clave] = iniciar_port_forward(DASHBOARDS[clave])
    except KeyboardInterrupt:
        print("\nCerrando port-forward...")
    finally:
        for proc in activos.values():
            proc.terminate()


def estado(_args):
    if not cluster_responde():
        fallar(f"El cluster no responde. ¿Está arrancado? Probá: {comando('crear')}")
    print(f"Contexto: {contexto_actual()}")
    print("\n── Applications ──")
    print(kubectl("get", "applications", "-n", "argocd", verificar=False).stdout)
    pods = kubectl("get", "pods", "-A", "--no-headers", verificar=False).stdout.splitlines()
    problemas = [p for p in pods if " Running " not in p and " Completed " not in p]
    print("── Pods con problemas ──")
    print("\n".join(problemas) if problemas else "ninguno")
    print("\n── Últimos workflows (namespace argo) ──")
    wf = kubectl("get", "workflows", "-n", "argo", "--sort-by=.metadata.creationTimestamp", "--no-headers",
                 verificar=False).stdout.splitlines()
    print("\n".join(wf[-5:]) if wf else "ninguno")


# ───────────────────────── Laboratorio (minikube) ─────────────────────────
def borrar(args):
    if shutil.which("minikube") is None:
        fallar("minikube no está instalado.")
    ctx = contexto_actual()
    if ctx not in ("", PERFIL_MINIKUBE):
        fallar(f"El contexto actual es '{ctx}', no '{PERFIL_MINIKUBE}'. Por seguridad no borro nada.")
    confirmar(f"Se va a BORRAR el cluster de minikube (perfil '{PERFIL_MINIKUBE}') con todo lo que tiene.", args)
    paso("Borrando el cluster")
    if subprocess.run(["minikube", "delete", f"--profile={PERFIL_MINIKUBE}"]).returncode != 0:
        fallar("minikube no pudo borrar el cluster.")
    print("   ✅ Cluster borrado")


def reiniciar(args):
    cargar_env(obligatorio=True)
    verificaciones_previas()      # primero lo que no toca nada: si el .env está mal, no se borra el cluster
    borrar(args)
    crear(args, verificado=True)


COMANDOS = {"crear": crear, "abrir": abrir, "estado": estado, "reiniciar": reiniciar, "borrar": borrar}


def main():
    sys.stdout.reconfigure(line_buffering=True)  # que los mensajes salgan al instante aunque no haya terminal
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help") or args[0] not in COMANDOS:
        print(__doc__)
        sys.exit(0 if not args or args[0] in ("-h", "--help", "help") else 1)
    COMANDOS[args[0]](args[1:])


if __name__ == "__main__":
    main()
