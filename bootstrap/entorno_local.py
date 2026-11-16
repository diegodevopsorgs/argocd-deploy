"""Levanta el entorno GitOps local en minikube. Ver README.md en la raíz de argocd-deploy.

    python entorno_local.py crear   # verifica credenciales, crea el cluster, instala Argo CD y abre las UIs
    python entorno_local.py abrir   # sólo abre las UIs (URLs, usuario, contraseña y token)
"""
import base64, os, socket, subprocess, sys, time, urllib.request

ORG = "diegodevopsorgs"
APP = "simple-python-app"
ARGOCD = "v3.6.1"
CTX = "minikube"  # todos los kubectl usan este contexto: nunca se toca otro cluster
AQUI = os.path.dirname(os.path.abspath(__file__))

# nombre, namespace, service, puerto local, puerto del service, url
UIS = [("Argo CD", "argocd", "argocd-server", 8080, 443, "https://localhost:8080"),
       ("Argo Workflows", "argo", "platform-argo-workflows-server", 2746, 2746, "http://localhost:2746"),
       ("Webhook GitHub", "argo-events", "github-eventsource-svc", 12000, 12000, "http://localhost:12000/push")]


def fallar(msg):
    sys.exit(f"\n❌ {msg}")


def kubectl(*args, entrada=None, check=True):
    r = subprocess.run(["kubectl", "--context", CTX, *args], input=entrada, capture_output=True, text=True)
    if check and r.returncode != 0:
        fallar(f"kubectl {args[0]} {args[1]} falló:\n{r.stderr}")  # sólo 2 args: los demás pueden ser tokens
    return r


def cargar_env():
    ruta = os.path.join(AQUI, ".env")  # junto al script; .gitignore lo excluye
    if not os.path.isfile(ruta):
        fallar(f"No encontré {ruta}: copiá .env.example como .env y completalo.")
    for linea in open(ruta, encoding="utf-8"):
        clave, _, valor = linea.strip().partition("=")
        if valor and not clave.startswith("#"):
            os.environ[clave] = valor.strip("\"'")
    for var in ("GH_TOKEN", "DOCKERHUB_USER", "DOCKERHUB_TOKEN"):
        if not os.environ.get(var):
            fallar(f"Falta {var} en {ruta}")
    print(f"🔑 Credenciales leídas de {ruta}")


def acepta(url, usuario, clave):
    auth = base64.b64encode(f"{usuario}:{clave}".encode()).decode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"}), timeout=20)
        return True
    except OSError:  # incluye HTTPError (401/403) y errores de red
        return False


def verificar_credenciales():
    # Es la misma consulta que hace `git push`. La API de GitHub dice "push: true" aunque el token no pueda.
    url = f"https://github.com/{ORG}/{APP}-conf.git/info/refs?service=git-receive-pack"
    if not acepta(url, "x-access-token", os.environ["GH_TOKEN"]):
        fallar(f"GH_TOKEN no puede escribir en {APP}-conf (el CI no podría publicar el tag).\n"
               f"   Token fine-grained: Resource owner = {ORG}, Contents = Read and write.")
    print(f"✅ GH_TOKEN puede escribir en {APP}-conf")

    usuario = os.environ["DOCKERHUB_USER"]
    url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{usuario}/{APP}:push,pull"
    if not acepta(url, usuario, os.environ["DOCKERHUB_TOKEN"]):
        fallar("Docker Hub rechazó DOCKERHUB_USER / DOCKERHUB_TOKEN (usá un access token dckr_pat_ Read & Write).")
    print("✅ Docker Hub acepta las credenciales")


def arrancar_minikube():
    if kubectl("get", "nodes", "--request-timeout=10s", check=False).returncode == 0:
        return print("✅ minikube ya está corriendo")
    # Si el contenedor desapareció (p. ej. al reiniciar Docker), el perfil viejo no arranca: se borra.
    if subprocess.run(["docker", "inspect", CTX], capture_output=True).returncode != 0:
        subprocess.run(["minikube", "delete", "-p", CTX], capture_output=True)
    print("⏳ Arrancando minikube (la primera vez tarda unos minutos)...")
    extra = [f"--{flag}={os.environ[var]}" for flag, var in [("cpus", "MINIKUBE_CPUS"), ("memory", "MINIKUBE_MEMORY")] if var in os.environ]
    if subprocess.run(["minikube", "start", "-p", CTX, "--driver=docker", *extra]).returncode != 0:
        fallar(f"minikube no arrancó. ¿Docker Desktop está abierto? Detalle: minikube logs -p {CTX}")
    kubectl("wait", "--for=condition=Ready", "node", "--all", "--timeout=180s")
    print("✅ minikube listo")


def instalar_argocd():
    print(f"⏳ Instalando Argo CD {ARGOCD}...")
    kubectl("create", "namespace", "argocd", check=False)  # si ya existe, no pasa nada
    kubectl("apply", "--server-side", "--force-conflicts", "-n", "argocd", "-f",
            f"https://raw.githubusercontent.com/argoproj/argo-cd/{ARGOCD}/manifests/install.yaml")
    kubectl("wait", "--for=condition=available", "deployment", "--all", "-n", "argocd", "--timeout=300s")
    print("✅ Argo CD listo")


def aplicar_secret(*args):
    yaml = kubectl("create", "secret", *args, "--dry-run=client", "-o", "yaml").stdout
    kubectl("apply", "-f", "-", entrada=yaml)


def crear_secrets():
    token, usuario, clave = os.environ["GH_TOKEN"], os.environ["DOCKERHUB_USER"], os.environ["DOCKERHUB_TOKEN"]
    kubectl("create", "namespace", "argo", check=False)
    # Argo CD lee con esta credencial todos los repos de la organización
    aplicar_secret("generic", "repo-creds-github", "-n", "argocd", "--from-literal=type=git",
                   f"--from-literal=url=https://github.com/{ORG}", "--from-literal=username=x-access-token",
                   f"--from-literal=password={token}")
    kubectl("label", "secret", "repo-creds-github", "-n", "argocd", "argocd.argoproj.io/secret-type=repo-creds", "--overwrite")
    # El CI clona el código y pushea el tag con el token, y sube la imagen con las credenciales de Docker Hub
    aplicar_secret("generic", "github-token", "-n", "argo", f"--from-literal=token={token}")
    aplicar_secret("docker-registry", "dockerhub-creds", "-n", "argo", "--docker-server=https://index.docker.io/v1/",
                   f"--docker-username={usuario}", f"--docker-password={clave}")
    print("✅ Secrets creados: repo-creds-github (argocd), github-token y dockerhub-creds (argo)")


def revisar_puerto(puerto):
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", puerto))
    except PermissionError:
        print(f"   ⚠️  Windows reservó el puerto {puerto}. En PowerShell como administrador: net stop winnat; net start winnat")
    except OSError:
        print(f"   ⚠️  El puerto {puerto} está ocupado. Ver quién lo usa: Get-NetTCPConnection -LocalPort {puerto}")


def token_workflows():
    # El ServiceAccount ui-viewer lo crea Argo CD desde Git unos minutos después de aplicar root-app: se espera.
    for intento in range(60):  # hasta 10 minutos
        r = kubectl("create", "token", "ui-viewer", "-n", "argo", "--duration=8h", check=False)
        if r.returncode == 0:
            return r.stdout.strip()
        if intento == 0:
            print("⏳ Esperando a que Argo CD instale Argo Workflows (para generar el token)...")
        time.sleep(10)
    fallar("Argo Workflows no se instaló en 10 minutos. Revisá platform-ci en la UI de Argo CD.")


def abrir():
    password = kubectl("get", "secret", "argocd-initial-admin-secret", "-n", "argocd", "-o", "jsonpath={.data.password}", check=False).stdout
    token = token_workflows()
    print(f"\n🌐 Argo CD         https://localhost:8080\n"
          f"   usuario: admin   password: {base64.b64decode(password).decode() if password else '(no disponible)'}\n"
          f"\n🌐 Argo Workflows  http://localhost:2746\n"
          f"   pegar en el login: Bearer {token}\n"
          f"\n🌐 Webhook GitHub  http://localhost:12000/push\n"
          f"   para que GitHub llegue, en otra terminal: cloudflared tunnel --url http://localhost:12000\n")
    for *_, puerto, _, _ in UIS:
        revisar_puerto(puerto)

    # Mantiene los port-forward abiertos: si uno no arranca (pod todavía levantando) o se corta, lo reabre.
    procesos = {}
    print("Port-forward activos. Ctrl+C para cerrar.")
    try:
        while True:
            for nombre, ns, svc, local, remoto, _ in UIS:
                p = procesos.get(nombre)
                if p is None or p.poll() is not None:
                    procesos[nombre] = subprocess.Popen(
                        ["kubectl", "--context", CTX, "port-forward", f"svc/{svc}", "-n", ns, f"{local}:{remoto}"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5)
    except KeyboardInterrupt:
        for p in procesos.values():
            p.terminate()


def crear():
    cargar_env()
    verificar_credenciales()
    arrancar_minikube()
    instalar_argocd()
    crear_secrets()
    # A partir de acá Argo CD instala todo lo demás desde Git (Workflows, Events, CI y la app)
    kubectl("apply", "-f", os.path.join(AQUI, "root-app.yaml"))
    print("✅ root-app aplicada: Argo CD instala el resto desde Git (tarda unos minutos)")
    abrir()


if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else ""
    if comando == "crear":
        crear()
    elif comando == "abrir":
        abrir()
    else:
        sys.exit(__doc__)
