"""Génère un GLB avec Microsoft TRELLIS (licence MIT) — à lancer avec le Python de TRELLIS.

Installation de TRELLIS (GPU NVIDIA ≥ 16 Go) : https://github.com/microsoft/TRELLIS
  git clone --recurse-submodules https://github.com/microsoft/TRELLIS
  cd TRELLIS && . ./setup.sh --new-env --basic --xformers --flash-attn --diffoctreerast --spconv --mipgaussian --kaolin --nvdiffrast

Usage :
  python trellis_generate.py --repo ~/TRELLIS --image perso.png --out perso.glb
  python trellis_generate.py --repo ~/TRELLIS --text "un pingouin cowboy" --out perso.glb
"""
import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="", help="dossier du dépôt TRELLIS")
    parser.add_argument("--image", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--texture-size", type=int, default=1024)
    args = parser.parse_args()

    if args.repo:
        sys.path.insert(0, os.path.expanduser(args.repo))
    os.environ.setdefault("SPCONV_ALGO", "native")

    from trellis.utils import postprocessing_utils

    if args.image:
        from PIL import Image
        from trellis.pipelines import TrellisImageTo3DPipeline

        print("Chargement de TRELLIS-image-large…", flush=True)
        pipeline = TrellisImageTo3DPipeline.from_pretrained("microsoft/TRELLIS-image-large")
        pipeline.cuda()
        print("Génération depuis l'image…", flush=True)
        outputs = pipeline.run(Image.open(args.image), seed=args.seed)
    elif args.text:
        from trellis.pipelines import TrellisTextTo3DPipeline

        print("Chargement de TRELLIS-text-xlarge…", flush=True)
        pipeline = TrellisTextTo3DPipeline.from_pretrained("microsoft/TRELLIS-text-xlarge")
        pipeline.cuda()
        print("Génération depuis le texte…", flush=True)
        outputs = pipeline.run(args.text, seed=args.seed)
    else:
        sys.exit("Donne --image ou --text")

    print("Export GLB…", flush=True)
    glb = postprocessing_utils.to_glb(outputs["gaussian"][0], outputs["mesh"][0],
                                      simplify=0.95, texture_size=args.texture_size)
    glb.export(args.out)
    print(f"OK {args.out}", flush=True)


if __name__ == "__main__":
    main()
