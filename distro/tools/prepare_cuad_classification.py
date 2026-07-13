#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path


def positive_answer_count(qa):
    return sum(1 for answer in qa.get("answers", []) if answer.get("text", "").strip())


def load_documents(input_path):
    with input_path.open() as f:
        payload = json.load(f)

    raw_documents = payload["data"]
    label_order = [qa["id"].split("__", 1)[1] for qa in raw_documents[0]["paragraphs"][0]["qas"]]
    question_text_by_label = {
        qa["id"].split("__", 1)[1]: qa["question"]
        for qa in raw_documents[0]["paragraphs"][0]["qas"]
    }

    documents = []
    for index, raw_document in enumerate(raw_documents):
        paragraph = raw_document["paragraphs"][0]
        context = paragraph["context"]
        qas = paragraph["qas"]
        label_targets = {}
        positive_labels = []
        answer_counts = {}

        for qa in qas:
            label = qa["id"].split("__", 1)[1]
            answers = positive_answer_count(qa)
            target = 1 if answers > 0 else 0
            label_targets[label] = target
            answer_counts[label] = answers
            if target:
                positive_labels.append(label)

        documents.append(
            {
                "document_index": index,
                "document_id": raw_document["title"],
                "title": raw_document["title"],
                "context": context,
                "context_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
                "char_length": len(context),
                "positive_label_count": len(positive_labels),
                "positive_labels": positive_labels,
                "label_targets": label_targets,
                "answer_counts": answer_counts,
            }
        )

    return documents, label_order, question_text_by_label


def assign_splits(documents, seed):
    ordered = sorted(documents, key=lambda item: item["document_id"])
    rng = random.Random(seed)
    rng.shuffle(ordered)

    total = len(ordered)
    train_end = int(total * 0.8)
    val_end = train_end + int(total * 0.1)

    split_map = {}
    for idx, document in enumerate(ordered):
        if idx < train_end:
            split = "train"
        elif idx < val_end:
            split = "val"
        else:
            split = "test"
        split_map[document["document_id"]] = split

    return split_map


def build_examples(documents, label_order, question_text_by_label, split_map):
    examples = []
    for document in documents:
        split = split_map[document["document_id"]]
        for label in label_order:
            examples.append(
                {
                    "split": split,
                    "document_index": document["document_index"],
                    "document_id": document["document_id"],
                    "label": label,
                    "question": question_text_by_label[label],
                    "target": document["label_targets"][label],
                    "answer_count": document["answer_counts"][label],
                    "char_length": document["char_length"],
                    "context_sha256": document["context_sha256"],
                }
            )
    return examples


def write_jsonl(path, rows):
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def write_examples_csv(path, rows):
    fieldnames = [
        "split",
        "document_index",
        "document_id",
        "label",
        "question",
        "target",
        "answer_count",
        "char_length",
        "context_sha256",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_summary(documents, examples, label_order, split_map, seed, input_path):
    split_document_counts = Counter(split_map.values())
    split_example_counts = Counter(example["split"] for example in examples)
    positive_by_split = {split: Counter() for split in split_document_counts}
    zero_positive_labels_by_split = defaultdict(list)

    for example in examples:
        if example["target"]:
            positive_by_split[example["split"]][example["label"]] += 1

    label_totals = Counter()
    for split, counter in positive_by_split.items():
        for label in label_order:
            value = counter[label]
            label_totals[label] += value
            if value == 0:
                zero_positive_labels_by_split[split].append(label)

    positive_label_count_distribution = Counter(
        document["positive_label_count"] for document in documents
    )

    return {
        "source_path": str(input_path),
        "seed": seed,
        "labels": label_order,
        "document_count": len(documents),
        "example_count": len(examples),
        "split_document_counts": dict(split_document_counts),
        "split_example_counts": dict(split_example_counts),
        "positive_label_count_distribution": dict(sorted(positive_label_count_distribution.items())),
        "positive_label_totals": {label: label_totals[label] for label in label_order},
        "positive_label_totals_by_split": {
            split: {label: positive_by_split[split][label] for label in label_order}
            for split in sorted(positive_by_split)
        },
        "zero_positive_labels_by_split": dict(zero_positive_labels_by_split),
    }


def main():
    parser = argparse.ArgumentParser(description="Prepare CUAD classification artifacts.")
    parser.add_argument("--input", required=True, help="Path to CUAD_v1.json")
    parser.add_argument("--output-dir", required=True, help="Directory for prepared artifacts")
    parser.add_argument("--seed", type=int, default=1337, help="Deterministic split seed")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    documents, label_order, question_text_by_label = load_documents(input_path)
    split_map = assign_splits(documents, args.seed)
    for document in documents:
        document["split"] = split_map[document["document_id"]]

    examples = build_examples(documents, label_order, question_text_by_label, split_map)
    summary = build_summary(documents, examples, label_order, split_map, args.seed, input_path)

    documents_path = output_dir / "documents.jsonl"
    examples_path = output_dir / "examples.jsonl"
    examples_csv_path = output_dir / "examples.csv"
    summary_path = output_dir / "summary.json"

    write_jsonl(documents_path, documents)
    write_jsonl(examples_path, examples)
    write_examples_csv(examples_csv_path, examples)
    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
        f.write("\n")

    print(json.dumps({
        "documents_path": str(documents_path),
        "examples_path": str(examples_path),
        "examples_csv_path": str(examples_csv_path),
        "summary_path": str(summary_path),
        "split_document_counts": summary["split_document_counts"],
        "split_example_counts": summary["split_example_counts"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
