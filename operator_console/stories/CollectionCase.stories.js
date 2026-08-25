import { renderCollectionCase } from "../src/collection_case.js";
import openCollectionCase from "../fixtures/open_collection_case.json";
import dunningCollectionCase from "../fixtures/dunning_collection_case.json";
import settledCollectionCase from "../fixtures/settled_collection_case.json";

export default {
  title: "CollectionCase",
};

export const OpenCollect = {
  render: () => renderCollectionCase(openCollectionCase),
};

export const DunningCollect = {
  render: () => renderCollectionCase(dunningCollectionCase),
};

export const SettledWait = {
  render: () => renderCollectionCase(settledCollectionCase),
};
